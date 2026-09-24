# -*- coding: utf-8 -*-
"""
从希沃服务器动态构建云班实例（全局名 `yunban_client`）。

从现有登录会话（tokens.json）推导：
- token：campus.seewo.com 的 acw_tc 反爬 cookie，经一次对 campus 的请求从响应 Cookie 捕获。
- schoolid：当前登录家长关联学生的 schoolUid。
"""

import requests
from login import acc
from stu import stu
from yunban import yunban
from init import proxies, verify, headers_nocookie


def _grab_acw_tc(account: acc) -> str:
    """访问 campus 用户状态接口，从响应 Cookie 中抓取 acw_tc cookie"""
    url = (
        "https://campus.seewo.com/soul-bootstrap/seewo-phoenix-blood-server/mobile/user/v1/"
        f"{account.uid}/functionality"
    )
    re = requests.get(
        url,
        headers=account.headers,
        proxies=proxies,
        verify=verify,
    )
    cookies = requests.utils.dict_from_cookiejar(re.cookies)
    acw = cookies.get("acw_tc", "")
    if not acw:
        # 部分网关 cookie 只在首次建连时下发，尝试额外收割一次
        requests.get(
            "https://campus.seewo.com",
            headers=headers_nocookie,
            proxies=proxies,
            verify=verify,
        )
    return acw


account = acc()  # 复用 tokens.json 登录会话，无 Token 时自动扫码
student = stu(account)  # 取关联的第一个学生
token = _grab_acw_tc(account)
schoolid = student.schoolUid

if not token:
    raise RuntimeError(
        "未从 campus.seewo.com 捕获到 acw_tc cookie，请确认登录会话有效、网络可达。"
    )

# 生产云班 yunban 实例（消费者经此对接 campus REST）
yunban_client = yunban(token=token, schoolid=schoolid)
