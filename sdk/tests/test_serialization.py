"""序列化必须永远成功，且对超长内容显式截断。"""

from agent_flight_recorder.serialization import MAX_STRING, to_jsonable, to_json_text


def test_long_string_is_truncated_with_marker() -> None:
    text = "x" * (MAX_STRING + 50)
    result = to_jsonable(text)
    assert "truncated 50 chars" in result
    assert len(result) < MAX_STRING + 100


def test_unknown_objects_fall_back_to_string() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque-value"

    assert to_jsonable({"thing": Opaque()})["thing"] == "opaque-value"


def test_bytes_are_summarized_not_dumped() -> None:
    assert to_jsonable(b"12345") == "<bytes:5>"


def test_deep_structures_stop_at_max_depth() -> None:
    payload: dict = {}
    cursor = payload
    for _ in range(20):
        cursor["next"] = {}
        cursor = cursor["next"]
    assert "<max-depth>" in to_json_text(payload)


def test_dataclass_like_objects_use_model_dump() -> None:
    from pydantic import BaseModel

    class Payload(BaseModel):
        value: int

    assert to_jsonable(Payload(value=3)) == {"value": 3}


def test_message_dict_handles_langchain_shapes() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from agent_flight_recorder.serialization import message_to_dict, text_of

    human = HumanMessage(content="hello")
    assert message_to_dict(human)["role"] == "user"

    ai = AIMessage(
        content="thinking",
        tool_calls=[{"name": "search", "args": {"q": "x"}, "id": "c1", "type": "tool_call"}],
    )
    payload = message_to_dict(ai)
    assert payload["role"] == "assistant"
    assert payload["tool_calls"][0]["name"] == "search"
    assert text_of(ai) == "thinking"

    tool = ToolMessage(content="result", tool_call_id="c1", status="success")
    assert message_to_dict(tool)["role"] == "tool"

