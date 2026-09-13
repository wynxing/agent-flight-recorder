"""最小脱敏。

Trace 里必然出现凭证、密钥和用户数据，因此脱敏不能等到后面补。规则同时用于
服务端入库与 SDK 本地预览，保证"平台看到的"和"本地看到的"一致。
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED:{rule}]"

# 键名精确命中即整体抹掉值。用精确匹配而不是子串匹配，避免把 token_usage 这类
# 正常字段误伤。
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "access_key",
        "secret_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "token",
        "authorization",
        "private_key",
        "session_id",
        "cookie",
        "set-cookie",
        "aws_secret_access_key",
    }
)

SENSITIVE_VALUE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}.[A-Za-z0-9_-]{10,}.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)bearers+[A-Za-z0-9-._~+/]{16,}=*")),
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
)


def _normalized_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def redact(value: Any) -> tuple[Any, list[str]]:
    """返回 (脱敏后的值, 命中的规则名列表)。不修改入参。"""

    hits: list[str] = []
    result = _walk(value, hits, depth=0)
    return result, sorted(set(hits))


def _walk(value: Any, hits: list[str], *, depth: int) -> Any:
    if depth > 12:
        return value
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            rule = _key_rule(key)
            if rule is not None and item not in (None, "", [], {}):
                hits.append(rule)
                cleaned[key] = REDACTED.format(rule=rule)
            else:
                cleaned[key] = _walk(item, hits, depth=depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_walk(item, hits, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value, hits)
    return value


def _key_rule(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    lowered = key.strip().lower()
    if lowered in SENSITIVE_KEYS:
        return "sensitive_key"
    if _normalized_key(lowered) in SENSITIVE_KEYS:
        return "sensitive_key"
    return None


def _redact_text(text: str, hits: list[str]) -> str:
    if not text:
        return text
    result = text
    for name, pattern in SENSITIVE_VALUE_RULES:
        def _replace(match: re.Match[str], _name: str = name) -> str:
            hits.append(_name)
            return REDACTED.format(rule=_name)

        result = pattern.sub(_replace, result)
    return result

