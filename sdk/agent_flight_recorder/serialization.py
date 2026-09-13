"""把 Agent 运行时对象转成可安全落库、可安全显示的 JSON 结构。

录制必须永远能成功：任何序列化失败都要降级成可读的字符串，而不是抛异常。
"""

from __future__ import annotations

import json
from typing import Any

MAX_STRING = 20000
MAX_ITEMS = 200


def _truncate(text: str) -> str:
    if len(text) <= MAX_STRING:
        return text
    return text[:MAX_STRING] + f"...[truncated {len(text) - MAX_STRING} chars]"


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """把任意对象转成 JSON 可序列化结构。超过深度或长度的部分被显式截断。"""

    if _depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        items = list(value.items())[:MAX_ITEMS]
        return {str(k): to_jsonable(v, _depth + 1) for k, v in items}
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_ITEMS]
        return [to_jsonable(v, _depth + 1) for v in items]

    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return to_jsonable(dump(mode="json"), _depth + 1)
        except Exception:
            try:
                return to_jsonable(dump(), _depth + 1)
            except Exception:
                pass

    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        try:
            return to_jsonable(as_dict(), _depth + 1)
        except Exception:
            pass

    return _truncate(str(value))


def to_json_text(value: Any) -> str:
    try:
        return _truncate(json.dumps(to_jsonable(value), ensure_ascii=False, default=str))
    except Exception:
        return _truncate(str(value))


def message_to_dict(message: Any) -> dict[str, Any]:
    """把 LangChain 消息压成稳定的展示/回放形状。"""

    if message is None:
        return {}
    if isinstance(message, dict):
        return to_jsonable(message)

    payload: dict[str, Any] = {
        "type": getattr(message, "type", type(message).__name__),
        "role": _role_of(message),
        "content": _content_of(message),
    }

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = [
            {
                "id": _get(call, "id"),
                "name": _get(call, "name"),
                "args": to_jsonable(_get(call, "args") or {}),
            }
            for call in tool_calls
        ]

    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id

    name = getattr(message, "name", None)
    if name:
        payload["name"] = name

    status = getattr(message, "status", None)
    if isinstance(status, str):
        payload["status"] = status

    usage = getattr(message, "usage_metadata", None)
    if usage:
        payload["usage"] = to_jsonable(usage)

    return payload


def _role_of(message: Any) -> str:
    msg_type = getattr(message, "type", None)
    return {
        "human": "user",
        "ai": "assistant",
        "system": "system",
        "tool": "tool",
        "function": "tool",
    }.get(msg_type, msg_type or "unknown")


def _content_of(message: Any) -> Any:
    content = getattr(message, "content", None)
    if content is None:
        return ""
    return to_jsonable(content)


def _get(call: Any, key: str) -> Any:
    if isinstance(call, dict):
        return call.get(key)
    return getattr(call, key, None)


def text_of(message: Any) -> str:
    """取出模型消息的纯文本，兼容 content 为分块列表的新格式。"""

    if message is None:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def usage_to_tokens(message: Any) -> dict[str, int] | None:
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    total = usage.get("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    payload = {
        "input": prompt,
        "output": completion,
        "total": total if total is not None else (prompt or 0) + (completion or 0),
    }
    return {k: v for k, v in payload.items() if v is not None}

