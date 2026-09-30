# -*- coding: utf-8 -*-
"""一次性登录并上传单个文件到希沃云存储（实现见 upload.upload_file）。"""

import os
from sys import argv

from login import acc
from upload import upload_file

if __name__ == "__main__":
    account = acc()
    file = argv[1]
    url = upload_file(account, file)
    print(f"{os.path.basename(file)}：{url}")
