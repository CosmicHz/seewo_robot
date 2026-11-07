"""平台适配层：把各平台协议翻译成平台中立模型。"""

from .seewo import SeewoProvider

__all__ = ["SeewoProvider"]
