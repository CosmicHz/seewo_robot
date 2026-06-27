# -*- coding: utf-8 -*-
"""
登录与账户管理

实现微信扫码登录流程：下载二维码 → 终端显示 → 轮询扫码状态 → 保存 Token。
acc 类封装账户凭证，提供 headers/mheaders 两种请求头分别用于不同的希沃 API 端点。
"""

from qrcode import print_qrcode
from init import token_file, qrcode_file, proxies, verify, headers_nocookie, urls
from funcs import load_json, write_file
import json
import logging
import requests
import time
import os

logger = logging.getLogger("seewo.login")


class acc:
    """账户对象，封装登录凭证和请求头

    Args:
        type: 0=从 tokens.json 加载已有凭证（默认），1=强制重新扫码登录
        auto_login: Token 过期时是否自动触发扫码登录。
                    main.py 中为 True（终端用户可直接扫码），
                    api_server.py 中为 False（避免服务端阻塞，由客户端引导扫码）

    Attributes:
        uid: 用户 ID
        headers: 用于 campus.seewo.com 接口的请求头
        mheaders: 用于 m-campus.seewo.com 接口的请求头
        token_expired: 当 auto_login=False 且 Token 无效时设为 True
    """

    def __init__(self, type=0, auto_login=True, max_retries = 3) -> None:
        self.token_expired = False
        info = None

        if type == 0:  # 检查缓存的登录凭据
            if not os.path.exists(token_file):
                if auto_login:
                    type = 1
                else:
                    self.token_expired = True
                    return None
            else:
                info = load_json(token_file)

        if type == 1:  # 直接扫码登录
            if not auto_login:
                self.token_expired = True
                return None
            login()
            info = load_json(token_file)

        self.uid = info["userId"]
        self._set_headers(info)
        # 检查登录是否成功，失败则重试
        
        for attempt in range(max_retries):
            if self.check_status():
                return None
            if not auto_login:
                self.token_expired = True
                return None
            # 重新扫码登录
            print(f"Token无效，重新登录 (第{attempt + 1}次)...")
            login()
            info = load_json(token_file)
            self.uid = info["userId"]
            self._set_headers(info)
        self.token_expired = True
        return None

    def _set_headers(self, info):
        """根据登录凭证设置请求头"""
        self.headers = {
            "x-info-sign": "",
            "user-agent": "Dart/2.18 (dart:io)",
            "accept": "application/json,*/*",
            "x-auth-app": "seewo-yunban-mobile",
            "x-auth-appcode": "seewo-yunban-mobile",
            "cookie": f"x-auth-appCode=seewo-yunban-mobile; x-auth-token={info['token']}; x-token={info['token']}",
            "accept-encoding": "gzip",
            "content-type": "application/json",
            "host": "campus.seewo.com",
        }
        self.mheaders = {
            "x-info-sign": "",
            "user-agent": "Mozilla/5.0 (Linux; Android 9; Nexus 5 Build/PQ3A.190801.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/81.0.4044.117 Mobile Safari/537.36",
            "accept": "application/json,*/*",
            "x-auth-app": "seewo-yunban-mobile",
            "x-auth-appcode": "seewo-yunban-mobile",
            "cookie": f"x-auth-appCode=seewo-yunban-mobile; x-auth-token={info['token']}; x-token={info['token']}",
            "accept-encoding": "gzip",
            "content-type": "application/json",
        }

    def status(self, re):
        """解析用户状态接口的返回值，判断 Token 是否有效

        Returns:
            True=Token 有效，False=Token 无效或过期
        """
        code = json.loads(re)["statusCode"]
        if code == -500:
            print("登录失败：token无效")
            return False
        elif code == -505:
            print("登录失败：token已过期")
            return False
        elif code == 200:
            return True
        else:
            print(re, end="\n")
            return False

    def check_status(self):
        """调用希沃用户状态接口验证当前 Token 是否有效"""
        url = urls().status + self.uid + "/functionality"
        logger.info("GET %s", url)
        re = requests.get(
            url,
            headers=self.headers,
            proxies=proxies,
            verify=verify,
        )
        logger.info("响应 status=%s body=%.200s", re.status_code, re.text)
        return self.status(re.text)


def get_cookies():
    """访问希沃登录页面获取初始 Cookie（用于后续二维码请求）"""
    re = requests.get(url=urls().login_api, headers=headers_nocookie, proxies=proxies)
    return requests.utils.dict_from_cookiejar(re.cookies)


def download_qrcode():
    """下载微信扫码登录二维码图片并保存到本地

    Returns:
        dict: 用于轮询扫码状态的 Cookie
    """
    re = requests.get(urls().qrcode_image, cookies=get_cookies(), proxies=proxies)
    content = re.content
    write_file(qrcode_file, content)
    return requests.utils.dict_from_cookiejar(re.cookies)


def check_qrcode(cookies):
    """查询二维码扫码状态

    Args:
        cookies: download_qrcode() 返回的 Cookie

    Returns:
        dict: 包含 statusCode 的扫码结果
            200=等待扫码, 201=已扫码待确认, 202=已确认(登录成功)
    """
    re = requests.get(
        urls().check_qrcode,
        headers=headers_nocookie,
        cookies=cookies,
        proxies=proxies,
    )
    return json.loads(re.text)


def login():
    """完整的扫码登录流程：下载二维码 → 终端显示 → 轮询状态 → 保存凭证

    Returns:
        True=登录成功，False=登录失败
    """
    cookies = download_qrcode()
    print_qrcode(qrcode_file)
    status = 200
    while status == 200 or status == 201:
        data = check_qrcode(cookies)["data"]
        status = data["statusCode"]
        message = data["message"]
        print(str(int(time.time())) + ": " + message + str(status), end="\r")
    else:
        if status == 202:
            write_file("tokens.json", json.dumps(data).encode())
            return True
        else:
            return False
