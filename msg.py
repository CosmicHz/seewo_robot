import requests
import json
import warnings
from funcs import pxdecode
from login import acc
from init import urls, proxies
from stu import stu
from api import api


class msg:
    def __init__(self, account: acc, student: stu) -> None:
        self.acc = account
        self.stu = student

    def get_last(self):
        """
        **极不完善，请勿使用**
        不知道是什么东西
        """
        # TODO: 异常处理
        warnings.warn("该方法极不完善，请勿使用", DeprecationWarning, stacklevel=2)
        re = requests.get(
            urls().get_last_msg + self.acc.uid,
            headers=self.acc.headers,
            proxies=proxies,
        )
        msg_o = json.loads(re.text)["data"]
        if msg_o is None:
            return "[ERROR] 无法获取消息体"
        if msg_o == []:
            return "[INFO] 无消息"
        elif msg_o[0]["lastMsgTips"] == "":
            return "[INFO] 消息为空"
        return msg_o[0]["lastMsgTips"]

    def get(self, count: int, start: int = 1):
        """获取留言列表（按 start 分页）

        生产实测：start 是 1-based 页码，start=1=最新一页，递增往更旧翻页；
        每页 count 条，页内按 id 升序（旧→新），页间无重叠。

        Args:
            count: 每页数量
            start: 页码，默认 1=最新一页

        Returns:
            dict: 响应数据（含 result 字段）
        """
        data = {
            "start": start,
            "pageSize": count,
            "parentUid": self.acc.uid,
            "childUid": self.stu.userUid,
        }
        return json.loads(
            pxdecode(
                api().action(
                    "GET_KIDNOTE_V1_BYPARENTUID_BYCHILDUID_NOTES", data, self.acc
                )
            )
        )

    def get_content(self, count: int):
        """
        **极不完善，请勿使用**
        获取最新消息内容
        """
        warnings.warn("该方法极不完善，请勿使用", DeprecationWarning, stacklevel=2)
        return self.get(count)["result"][0]["content"]

    def send(self, content: str, type: int, resUrl="", voiceLength=0, resConfig=""):
        data = {
            "schoolUid": self.stu.schoolUid,
            "classUid": self.stu.classUid,
            "senderUid": self.acc.uid,
            "receiverUid": self.stu.userUid,
            "senderType": "parent",
            "type": type,
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
        post = api().action("POST_KIDNOTE_V1_NOTE", data, self.acc)
        code = post["statusCode"]
        if code == -500:
            print("发送失败")
            return False
        elif code == 200:
            print("发送成功：" + content)
            return True
        else:
            print(f"unknown error: {post}")
            return False

    def get_id(self, count: int) -> int:
        result = self.get(count)["result"]
        if not result:
            return 0
        return result[0]["id"]

    def get_all_ids(self, count: int) -> list[int]:
        """获取多条消息的ID列表，按时间正序排列（旧→新）

        内部按 ID 排序，不依赖服务器返回的原始顺序。
        """
        result = self.get(count).get("result", [])
        ids = [m["id"] for m in result]
        return sorted(ids)

    def get_content_by_index(self, count: int, index: int) -> str:
        """获取第index条消息的内容（0=最旧，按ID升序）"""
        result = self.get(count)["result"]
        result = sorted(result, key=lambda m: m.get("id", 0))
        return result[index]["content"]

    def get_msg_detail(self, count: int, index: int) -> dict:
        """获取指定序号消息的完整信息（0=最旧，按ID升序）"""
        result = self.get(count)["result"]
        result = sorted(result, key=lambda m: m.get("id", 0))
        return result[index]

    def delete(self, count: int):
        id = self.get_id(count)
        data = {"ids": [id]}
        post = api().action("DELETE_KIDNOTE_V1_NOTE", data, self.acc)
        code = post["statusCode"]
        if code == -500:
            print("删除失败")
            return False
        elif code == 200:
            print("删除成功：")
            return True
        else:
            print("unknown error:")
            print(post)
