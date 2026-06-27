# -*- coding: utf-8 -*-
"""
全局常量与配置

定义项目级别的文件路径、网络代理、公共请求头和希沃 API URL。
统一从 config.json 读取配置，其他模块通过 from init import config 使用。
"""

import time
import json
import os

CONFIG_FILE = "config.json"


def load_config() -> dict:
    """加载配置文件，失败时返回空字典"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# 全局配置字典（只读使用，不要修改）
config = load_config()

# 二维码图片保存路径（登录时生成）
qrcode_file = "qrcode.png"
# 登录凭证存储路径（含 userId 和 token 等，实质上是登录时希沃服务器响应的内容）
token_file = "tokens.json"
# 上传文件记录存储路径
uploads_file = "uploads.json"
if not os.path.isfile(uploads_file):
    with open(uploads_file, "wb") as f:
        f.write(b"{}")

# Mock 模式相关（从 config 读取）
_use_mock = config.get("use_mock", False)
_mock_port = config.get("mock_port", 9000)
_mock_base = f"http://localhost:{_mock_port}"

# HTTP 代理配置，空字典表示不使用代理
proxies: dict[str, str] = {}  # type: ignore
# SSL 证书验证开关
verify = True

# 无 Cookie 的公共请求头，用于登录流程（获取二维码、检查扫码状态）
headers_nocookie = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Accept": "image/avif,image/webp,*/*",
    "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://id.seewo.com/login-iframe?system=mis-admin&callbackIframeUrl=%2F%2Fcampus.seewo.com%2Fcallback-iframe&redirect_url=",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
}


class urls:
    """希沃云班 API 端点集合

    部分端点 URL 包含时间戳参数（毫秒级），每次实例化时自动生成。
    当 config.json 中 use_mock=true 时，所有 URL 指向本地 mock 服务器。
    """

    def __init__(self) -> None:
        # 毫秒时间戳
        self.time = str(int(time.time()) * 1000)

        if _use_mock:
            base = _mock_base
            self.status = f"{base}/soul-bootstrap/seewo-phoenix-blood-server/mobile/user/v1/"
            self.get_last_msg = f"{base}/home-school-service/mobile/kidnote/v1/note/dialogs?userUid="
            self.api = f"{base}/class/apis.json?action="
            self.login_api = f"{base}/auth/loginApi?_time" + self.time
            self.qrcode_image = f"{base}/scan/qrcode?oriSys=mis-admin&t=" + self.time
            self.check_qrcode = f"{base}/scan/pcCheckQrcode?type=long&_=" + self.time
        else:
            self.status = "https://campus.seewo.com/soul-bootstrap/seewo-phoenix-blood-server/mobile/user/v1/"
            self.get_last_msg = "https://campus.seewo.com/home-school-service/mobile/kidnote/v1/note/dialogs?userUid="
            self.api = "https://m-campus.seewo.com/class/apis.json?action="
            self.login_api = "https://id.seewo.com/auth/loginApi?_time" + self.time
            self.qrcode_image = (
                "https://id.seewo.com/scan/qrcode?oriSys=mis-admin&t=" + self.time
            )
            self.check_qrcode = (
                "https://id.seewo.com/scan/pcCheckQrcode?type=long&_=" + self.time
            )
