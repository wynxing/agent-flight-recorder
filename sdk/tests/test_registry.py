"""AgentSpec / SeedCase：Agent 向平台声明自己的契约。"""

from __future__ import annotations

from agent_flight_recorder import AgentSpec, SeedCase


def _spec(**overrides) -> AgentSpec:
    base: dict = {"name": "demo", "build": lambda **_: None}
    base.update(overrides)
    return AgentSpec(**base)


def test_seed_cases_defaults_to_empty() -> None:
    """没声明自带用例的 Agent，不该被凭空塞进任何用例。"""

    assert _spec().seed_cases == []


def test_seed_case_defaults_are_permissive() -> None:
    case = SeedCase(name="只给名字也能建")

    assert case.assertions == []
    assert case.description == ""
    assert case.from_seq is None
    assert case.to_seq is None
    assert case.preset is None
    assert case.model is None
    assert case.system_prompt is None
    assert case.labels == {}


def test_describe_keeps_its_payload_shape() -> None:
    """/v1/agents 的载荷形状是对外契约，新增字段不能顺手把它改掉。"""

    payload = _spec(seed_cases=[SeedCase(name="x")]).describe()

    assert set(payload) == {
        "name",
        "description",
        "version",
        "default_model",
        "default_system_prompt",
        "prompt_presets",
        "tools",
    }


def test_positional_construction_is_unchanged() -> None:
    """seed_cases 追加在字段末尾，用位置参数构造的老写法必须继续可用。"""

    spec = AgentSpec("demo", lambda **_: None, None, "描述", "1.0")

    assert spec.description == "描述"
    assert spec.version == "1.0"
    assert spec.seed_cases == []
