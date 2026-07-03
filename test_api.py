# -*- coding: utf-8 -*-
"""
API 端点测试工具 - 逐个测试所有 API 端点，返回响应
"""

import json
import sys
import requests

from init import config

API_KEY = config.get("api_key", "your-secret-key")
BASE_URL = f"http://127.0.0.1:{config.get('api_port', 5000)}"
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def test_endpoint(method, path, label, **kwargs):
    url = f"{BASE_URL}{path}"
    print(f"\n{'='*60}")
    print(f"[{label}] {method} {path}")
    print(f"{'='*60}")
    try:
        if method == "GET":
            resp = requests.get(url, headers=HEADERS, timeout=10, **kwargs)
        else:
            resp = requests.post(url, headers=HEADERS, timeout=10, **kwargs)
        print(f"Status: {resp.status_code}")
        try:
            data = resp.json()
            # 消息列表只打印摘要，避免刷屏
            msgs = data.get("messages", [])
            if msgs:
                data["messages"] = [
                    {k: v for k, v in m.items() if k != "resUrl"} for m in msgs
                ]
                print(f"Messages count: {len(msgs)}")
                for i, m in enumerate(msgs):
                    print(f"  [{i}] id={m.get('id')}, sender={m.get('sender')}, "
                          f"senderName={m.get('senderName')}, "
                          f"type={m.get('type')}, content={str(m.get('content',''))[:60]}")
                data.pop("messages", None)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            print(resp.text[:2000])
    except Exception as e:
        print(f"ERROR: {e}")


def main():
    # 1. 状态检查
    test_endpoint("GET", "/api/status", "1. 状态检查")

    # 2. 登录二维码
    test_endpoint("GET", "/api/login/qrcode", "2. 登录二维码")

    # 3. 登录状态
    test_endpoint("GET", "/api/login/status", "3. 登录状态")

    # 4. 获取最新消息
    test_endpoint("GET", "/api/messages?count=10", "4. 获取最新消息")

    # 5. 本地历史记录
    test_endpoint("GET", "/api/history?limit=10", "5. 本地历史记录")

    # 6. 加载更早消息
    test_endpoint("GET", "/api/load_earlier?count=10", "6. 加载更早消息")

    # 7. 全量同步（短超时，可能耗时较长）
    print(f"\n{'='*60}")
    print("[7. 全量同步] POST /api/sync_all")
    print(f"{'='*60}")
    try:
        resp = requests.post(
            f"{BASE_URL}/api/sync_all",
            headers=HEADERS,
            json={"batch_size": 10, "delay": 0.5},
            timeout=120,
        )
        print(f"Status: {resp.status_code}")
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"ERROR: {e}")

    # 8. 全量同步后再查历史
    test_endpoint("GET", "/api/history?limit=10", "8. 同步后历史记录")

    # 9. 发送消息（跳过，避免误发）
    print(f"\n{'='*60}")
    print("[9. 发送消息] POST /api/send - 跳过（避免误发）")
    print(f"{'='*60}")

    # 10. 发送图片（跳过）
    print(f"\n{'='*60}")
    print("[10. 发送图片] POST /api/send_image - 跳过")
    print(f"{'='*60}")

    # 11. 发送音频（跳过）
    print(f"\n{'='*60}")
    print("[11. 发送音频] POST /api/send_audio - 跳过")
    print(f"{'='*60}")

    # 12. 刷新会话
    test_endpoint("POST", "/api/refresh", "12. 刷新会话")

    # 13. 执行命令
    test_endpoint("POST", "/api/execute", "13. 执行命令", json={"command": "status"})


if __name__ == "__main__":
    main()
