"""工具副作用声明。

回放安全完全建立在这个声明之上：没有声明，平台就不知道哪些工具可以在回放里
真实执行。因此声明机制必须便宜到"顺手就写了"，并且有明确的兜底默认值。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from .models import SideEffect

AFR_SIDE_EFFECT_ATTR = "afr_side_effect"

F = TypeVar("F", bound=Callable[..., Any])


def afr_tool(side_effect: SideEffect = SideEffect.READ) -> Callable[[F], F]:
    """给工具函数或工具对象打上副作用等级。

    两种写法都支持::

        @tool
        @afr_tool(SideEffect.WRITE)
        def kubectl_delete(...): ...

        @afr_tool(SideEffect.EXTERNAL)
        @tool
        def notify_oncall(...): ...
    """

    def decorator(target: F) -> F:
        try:
            setattr(target, AFR_SIDE_EFFECT_ATTR, side_effect)
        except (AttributeError, TypeError):  # pragma: no cover - 极少数不可写对象
            pass
        return target

    return decorator


def resolve_side_effect(
    tool: Any,
    name: str | None,
    *,
    mapping: dict[str, SideEffect] | None = None,
    default: SideEffect = SideEffect.READ,
) -> SideEffect:
    """按 显式映射 > 工具自身声明 > 默认值 的顺序解析副作用等级。"""

    if name and mapping and name in mapping:
        return mapping[name]

    candidates = [tool, getattr(tool, "func", None), getattr(tool, "coroutine", None)]
    for candidate in candidates:
        value = getattr(candidate, AFR_SIDE_EFFECT_ATTR, None)
        if value is not None:
            return _coerce(value, default)

    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get(AFR_SIDE_EFFECT_ATTR)
        if value is not None:
            return _coerce(value, default)

    return default


def _coerce(value: Any, default: SideEffect) -> SideEffect:
    if isinstance(value, SideEffect):
        return value
    try:
        return SideEffect(str(value))
    except ValueError:
        return default

