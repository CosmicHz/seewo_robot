"""平台中立的异常体系。

分层约定：
- `SeewoError` 是所有本项目异常的根，只在 `seewo/` 包内使用。
- `ProviderError` 系列表示**协议适配层**的失败，携带平台无关的语义
  （如 `auth_expired`），上层据此决定是否重试，**不需要知道希沃的状态码**。
- 具体的希沃业务码 → 平台的映射在 `seewo/providers/seewo.py` 内完成；
  上层永远不出现 `-500` / `40000` 这类字面量。

这样做的目的：换平台时只需要新写一个 Provider，核心层与接口层零改动。
"""

from __future__ import annotations


class SeewoError(Exception):
    """本项目所有异常的根类。"""


# ---------------------------------------------------------------------------
# 契约层
# ---------------------------------------------------------------------------


class ContractError(SeewoError):
    """违反 Provider / Store 等接口契约。"""


class CapabilityError(ContractError):
    """请求了平台不具备的能力。

    **不允许伪造能力**：平台发不了语音消息，就抛这个异常，而不是静默丢弃
    或降级成文本。消息体里带 `capability` 便于调用方定位。
    """

    def __init__(self, capability: str, detail: str = "") -> None:
        self.capability = capability
        message = f"平台不支持能力: {capability}"
        if detail:
            message += f"（{detail}）"
        super().__init__(message)


class ProviderUnsupportedError(CapabilityError):
    """Provider 不支持某项操作（别名，语义更贴近 Provider 视角）。"""


# ---------------------------------------------------------------------------
# Provider 层
# ---------------------------------------------------------------------------


class ProviderError(SeewoError):
    """Provider 执行失败。

    Attributes:
        retryable: 失败是否值得重试。认证失效、业务拒绝等属于不可重试
            （重试也不会成功），网络抖动属于可重试。
    """

    #: 类级默认值；实例可通过 `retryable=` 覆盖
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class ProviderConnectionError(ProviderError):
    """与上游的连接失败（网络、连接中断）。可重试。"""

    retryable = True


class ProviderAuthError(ProviderError):
    """凭证无效 / 过期 / 被吊销。需重新认证，不可盲目重试。

    核心层对它的处理是「刷新凭证后重试一次」（见 `MessageService.send`），
    仍失败则向调用方抛出 `SessionExpiredError`。
    """

    retryable = False


class ProviderBusinessError(ProviderError):
    """上游业务规则拒绝（如内容超长、目标不存在）。重试无意义。"""

    retryable = False


class ProviderTimeoutError(ProviderConnectionError):
    """上游超时。"""


# ---------------------------------------------------------------------------
# 核心服务层
# ---------------------------------------------------------------------------


class ServiceError(SeewoError):
    """核心服务层错误。"""


class SessionExpiredError(ServiceError):
    """会话已失效且无法自愈，调用方需重新认证。

    对应旧接口里 HTTP 401 + `need_login: true` 的语义。
    """


class DuplicateMessage(ServiceError):
    """消息已被处理（去重命中）。仅在显式要求"必须投递"时抛出。"""


class StoreError(ServiceError):
    """持久化失败。"""


class CorruptStoreError(StoreError):
    """持久化文件存在但内容损坏。

    与旧 `funcs.CorruptChatHistoryError` 语义一致：故意不吞异常，
    避免把"损坏"当成"空"进而覆盖清零历史。
    """


class InvalidStateTransition(ServiceError):
    """非法的连接状态跃迁。"""


class EventDispatchError(ServiceError):
    """事件处理器抛异常。"""
