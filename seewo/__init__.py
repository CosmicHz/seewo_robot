"""seewo —— 平台中立的通用消息服务。

本包是「业务与协议解耦」的核心：

- `seewo.models`  平台中立模型（消息/段/用户/会话/事件/能力/状态）
- `seewo.errors`  异常体系
- `seewo.provider` Provider 契约 —— **平台接入的唯一扩展点**
- `seewo.core`    核心服务层（去重/顺序/游标恢复/并发/状态机/事件分发）
- `seewo.providers` 具体平台实现（目前：希沃）
- `seewo.integrations` 第三方框架协议适配（OneBot 11 / Satori）
- `seewo.interfaces` 对外 HTTP 接口
- `seewo.sdk`     类型化 Python SDK

**依赖方向严格向下**：`interfaces` → `integrations` → `core` → `provider` → `models`。
`core` 与 `models` 不 import 任何平台专有符号 —— 由
`tests/test_layering.py` 自动守卫。
"""

from .core.service import MessageService
from .core.store import JsonStore, MemoryStore, Store
from .errors import (
    CapabilityError,
    ProviderAuthError,
    ProviderBusinessError,
    ProviderError,
    SeewoError,
    SessionExpiredError,
)
from .models import (
    Conversation,
    ConversationType,
    Event,
    EventType,
    Message,
    PlatformCapabilities,
    Role,
    Segment,
    SegmentType,
    User,
)
from .provider.base import OutgoingMessage, PollResult, Provider

__version__ = "2.0.0"

__all__ = [
    "CapabilityError",
    "Conversation",
    "ConversationType",
    "Event",
    "EventType",
    "JsonStore",
    "MemoryStore",
    "Message",
    "MessageService",
    "OutgoingMessage",
    "PlatformCapabilities",
    "PollResult",
    "Provider",
    "ProviderAuthError",
    "ProviderBusinessError",
    "ProviderError",
    "Role",
    "SeewoError",
    "Segment",
    "SegmentType",
    "SessionExpiredError",
    "Store",
    "User",
    "__version__",
]
