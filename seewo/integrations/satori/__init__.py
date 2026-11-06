"""Satori 协议适配。

本包把「平台中立的核心服务」暴露为 Satori 协议实现端，
供 Koishi / AIRI / 其它 Satori 客户端接入。

用法::

    from seewo.integrations.satori import SatoriAdapter, SatoriServer

    adapter = SatoriAdapter(service)
    server = SatoriServer(adapter, app)   # app 为 Flask app
"""

from .adapter import SatoriAdapter
from .server import SatoriServer

__all__ = ["SatoriAdapter", "SatoriServer"]
