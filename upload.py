# -*- coding: utf-8 -*-

import os
import json
import time
import random
import string
import mimetypes
import requests
from requests_toolbelt import MultipartEncoder
from init import uploads_file
from funcs import read_file, write_file
from login import acc
from api import api


def upload_file(account: acc, file, type=None) -> str:
    """上传文件到希沃云存储，返回 downloadUrl（失败返回空串）。

    这是上传能力的**唯一实现**：main.py / upload_file.py / api_server.py 都调这里，
    不要在各自模块里重复封装。`type=None` 时由 Upload.upload 按扩展名推导 MIME。
    """
    up = Upload(account)
    up.upload(file=file, type=type)
    return up.downloadUrl


class Upload:
    def __init__(self, account: acc) -> None:
        self.isupload = False
        self.acc = account
        # 失败时保持空串：调用方按 falsy 判失败，不必捕获异常
        self.downloadUrl = ""
        self.res = None
        self.error = ""
        self.get_resource()

    def get_resource(self):
        data = {"appId": "10388", "clientIp": "", "clientId": "", "requestId": ""}
        try:
            self.res = api().action(
                "POST_MOBILE_V1_RESOURCE_CSTORE_UPLOADPOLICY", data, self.acc
            )
            self.uploadUrl = self.res["data"]["policyList"][0]["uploadUrl"]
            self.expiretime = int(time.time()) + self.res["data"]["expireSeconds"]
            self.headers = {
                "Host": self.uploadUrl[8:],
                "Accept": "*/*",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Site": "cross-site",
                "Accept-Language": "zh-CN,zh-Hans;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Fetch-Mode": "cors",
                "Origin": "https://m-campus.seewo.com",
                "User-Agent": "Mozilla/5.0 (iPad; CPU OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                "Referer": "https://m-campus.seewo.com/",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "empty",
            }
        except json.decoder.JSONDecodeError:
            self.error = f"策略响应非 JSON: {str(self.res)[:200]}"
        except KeyError as e:
            # 业务失败（如 token 无效）响应里没有 data/policyList
            self.error = f"策略响应缺少字段 {e}: {str(self.res)[:200]}"
        except requests.RequestException as e:
            self.error = f"策略请求失败: {e}"
        except Exception as e:
            self.error = f"策略解析异常: {e}"
        if self.error:
            print("上传不可用: " + self.error)
        return None

    def upload(self, file, type=None):
        """上传文件到云存储。

        Args:
            file: 本地文件路径
            type: 显式 MIME 类型；为 None 时按扩展名推导（stdlib mimetypes），
                  推不出时回落 application/octet-stream
        """
        if self.isupload:
            print("该文件已上传: " + self.downloadUrl)
            return None
        if not hasattr(self, "uploadUrl"):
            print("上传失败：未取得上传策略")
            return None
        if type is None:
            type = mimetypes.guess_type(file)[0] or "application/octet-stream"
        response = None
        try:
            with open(file, "rb") as file_handle:
                data = {
                    "key": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][0]["value"],
                    ),
                    "policy": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][1]["value"],
                    ),
                    "q-signature": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][2]["value"],
                    ),
                    "q-key-time": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][3]["value"],
                    ),
                    "q-ak": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][4]["value"],
                    ),
                    "q-sign-algorithm": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][5]["value"],
                    ),
                    "callback": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][6]["value"],
                    ),
                    "success_action_status": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][7]["value"],
                    ),
                    "x:appid": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][8]["value"],
                    ),
                    "x:sessionid": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][9]["value"],
                    ),
                    "x:bucketid": (
                        None,
                        self.res["data"]["policyList"][0]["formFields"][10]["value"],
                    ),
                    "file": (
                        "IMG_" + str(random.randrange(0, 1000)) + ".PNG",
                        file_handle,
                        type,
                    ),
                }
                boundary = "----WebKitFormBoundary" + "".join(
                    random.sample(string.ascii_letters + string.digits, 16)
                )
                multipart = MultipartEncoder(fields=data, boundary=boundary)
                self.headers["Content-Type"] = multipart.content_type
                response = requests.post(
                    self.uploadUrl,
                    headers=self.headers,
                    data=multipart,
                    timeout=(self.expiretime - time.time()),
                )
                uploaded = json.loads(response.text)
                if uploaded["code"] == 0:
                    self.isupload = True
                    self.downloadUrl = uploaded["data"]["downloadUrl"]
                    uploads = json.loads(read_file(uploads_file))
                    filename = os.path.basename(file)
                    ledger_key = filename
                    collision = 2
                    while ledger_key in uploads:
                        ledger_key = f"{filename}#{collision}"
                        collision += 1
                    record = dict(uploaded["data"])
                    record.setdefault("filename", filename)
                    uploads[ledger_key] = record
                    write_file(
                        uploads_file,
                        json.dumps(
                            uploads, indent=4, sort_keys=True, ensure_ascii=False
                        ).encode(),
                    )
                    print("上传成功: " + self.downloadUrl)
                else:
                    print("上传失败: " + response.text)
        except Exception as e:
            self.error = f"上传请求异常: {e}"
            print("上传异常: " + self.error)
        return response
