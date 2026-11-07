"""第三方框架集成层。

每个子包实现一个协议适配器。它们只做**协议翻译**，
连接生命周期、去重、顺序、持久化等全部由 `seewo.core` 提供。
"""

from .base import IntegrationConfig, IntegrationServer, TranslationError

__all__ = ["IntegrationConfig", "IntegrationServer", "TranslationError"]
