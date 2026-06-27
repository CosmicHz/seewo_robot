import json
import logging
from init import verify, config
import requests
from login import acc
from funcs import pxdecode

logger = logging.getLogger("seewo.api")


def _get_api_base():
    """获取 m-campus API 基础 URL（支持 mock 模式）"""
    if config.get("use_mock"):
        port = config.get("mock_port", 9000)
        return f"http://localhost:{port}"
    return "https://m-campus.seewo.com"


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
        base = _get_api_base()
        url = f"{base}/class/apis.json?action=" + type
        encode_data = {"action": type, "params": params}
        logger.info("POST %s action=%s", url, type)
        re = requests.post(
            url,
            headers=account.mheaders,
            data=json.dumps(encode_data),
            verify=verify,
        )
        # 尝试解密响应体再输出
        try:
            resp_json = json.loads(re.text)
            if isinstance(resp_json, dict) and "data" in resp_json:
                decoded = pxdecode(resp_json)
                if isinstance(decoded, bytes):
                    decoded = decoded.decode("utf-8")
                logger.info("响应 %s status=%s 解密=%.300s", url, re.status_code, decoded)
            else:
                logger.info("响应 %s status=%s body=%.200s", url, re.status_code, re.text)
        except Exception:
            logger.info("响应 %s status=%s body=%.200s", url, re.status_code, re.text)
        return json.loads(re.text)
