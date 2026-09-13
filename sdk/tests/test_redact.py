"""脱敏规则。"""

from agent_flight_recorder.redact import redact


def test_sensitive_keys_are_masked() -> None:
    cleaned, hits = redact({"password": "hunter2", "api_key": "abc", "note": "keep"})
    assert cleaned["password"] == "[REDACTED:sensitive_key]"
    assert cleaned["api_key"] == "[REDACTED:sensitive_key]"
    assert cleaned["note"] == "keep"
    assert hits == ["sensitive_key"]


def test_nested_and_listed_values_are_masked() -> None:
    payload = {"outer": {"authorization": "Bearer abc"}, "items": [{"token": "xyz"}]}
    cleaned, hits = redact(payload)
    assert cleaned["outer"]["authorization"] == "[REDACTED:sensitive_key]"
    assert cleaned["items"][0]["token"] == "[REDACTED:sensitive_key]"
    assert hits == ["sensitive_key"]


def test_value_patterns_are_masked() -> None:
    cleaned, hits = redact({"note": "key sk-abcdefghijklmnopqrstuv and ghp_abcdefghijklmnopqrstuvwx"})
    assert cleaned["note"] == "key [REDACTED:openai_key] and [REDACTED:github_token]"
    assert set(hits) == {"openai_key", "github_token"}


def test_private_key_block_is_masked() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    cleaned, hits = redact({"pem": block})
    assert cleaned["pem"] == "[REDACTED:private_key_block]"
    assert hits == ["private_key_block"]


def test_ordinary_fields_are_untouched() -> None:
    payload = {"token_usage": {"input": 12}, "total_tokens": 30, "status": "ok"}
    cleaned, hits = redact(payload)
    assert cleaned == payload
    assert hits == []


def test_redaction_does_not_mutate_input() -> None:
    payload = {"password": "secret"}
    redact(payload)
    assert payload["password"] == "secret"

