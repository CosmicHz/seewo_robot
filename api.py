import json
from init import verify
import requests
from login import acc


class api:
    """m-campus API 调用网关"""

    def __init__(self) -> None:
        pass

    def action(self, type: str, params: dict, account: acc) -> dict:
        """调用 m-campus 统一 API

        Args:
            type: API 动作名，如 "GET_STUDENT_V1_PARENT_BYPARENTID_CHILDREN_LIST"
            params: 请求参数（通常需要先 pxencode 编码）
            account: 账户对象，提供 mheaders 请求头

        Returns:
            dict: 原始响应
        """
        encode_data = {"action": type, "params": params}
        re = requests.post(
            "https://m-campus.seewo.com/class/apis.json?action=" + type,
            headers=account.mheaders,
            data=json.dumps(encode_data),
            verify=verify,
        )
        return json.loads(re.text)
