"""运行时对象 -> 稳定标识。框架无关：只做鸭子类型读取，不导入任何框架。

这些函数原先长在 middleware.py 里，而那个模块在顶部 import langchain。结果是
第二个框架（OpenAI Agents SDK）想复用同一套「模型标识怎么取」时，要么抄一份、
要么被迫拖进 LangGraph——共享逻辑被关在了某个框架的模块里，这本身就是
「框架无关」没有落干净的一处。因此把它们移到本模块，middleware 只做转发。
"""

from __future__ import annotations

from typing import Any


def model_identifier(model: Any) -> str | None:
    """尽力取到模型的稳定标识。

    不同的模型实现把模型名放在不同的地方，自定义模型往往只声明
    _identifying_params。取不到时才退回类名。
    """

    if model is None:
        return None
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    params = getattr(model, "_identifying_params", None)
    if isinstance(params, dict):
        for key in ("model", "model_name", "model_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(model, str):
        return model
    return type(model).__name__


__all__ = ["model_identifier"]
