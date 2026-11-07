"""OneBot 11 协议适配。

本包把「平台中立的核心服务」暴露为 OneBot 11 标准实现端，
供 NoneBot2 / AstrBot / LangBot / NapCat 等框架接入。

用法::

    from seewo.integrations.onebot11 import OneBot11Adapter, OneBot11Server

    adapter = OneBot11Adapter(service)
    server = OneBot11Server(adapter, app)   # app 为 Flask app
"""

from .adapter import OneBot11Adapter
from .server import OneBot11Server

__all__ = ["OneBot11Adapter", "OneBot11Server"]
