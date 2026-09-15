"""请求契约：执行前提必须**要么生效、要么 422**，不许静默降级。

这个文件守的是一整类问题，而不是某一个端点。审核在真机上抓到过三处，都是同一个形状：

    调用方给了执行前提 -> 服务端丢了它 -> 200 -> 实际按**别的**前提执行

* POST /v1/runs/{id}/replay 带 systemPrompt（camelCase 拼写）：200，而计划里的 system_prompt
  是 null——想改 Prompt，实际按默认 Prompt 计划；
* POST /v1/suites 带 conditions=[{modle: "x"}]：200，条件变成 prompt=null, model=null——整列
  变成「沿用用例自身」，调用方以为在验证新模型；
* budget={max_model_call: 1} / policy={defualt: "live"}：200，预算变成「不设上限」、策略变成
  recorded——**想限制花费却变成不设上限**，想放行副作用却什么都没变。

这类缺陷的代价不对称：它看起来一切正常。因此这里的每条断言都是**走 HTTP 的**，不看 helper
——helper 层证明不了路由契约（这一课上一轮已经吃过一次）。

## 严格放在边界，不放在 SDK

严格的变体是 schemas.BudgetRequest / PolicyRequest（以及各请求模型自己的 extra="forbid"），
**没有**动 SDK 的 ReplayBudget / EffectPolicy。理由有两条，第二条是硬的：

1. 它们是公开导出（agent_flight_recorder.__all__），收口等于对所有使用者来一次破坏性变更；
2. 它们的**读取路径**会从库里 model_validate 存下来的策略（storage.run_to_record、case_to_item、
   cases._policy）。历史行里只要有一个本版本不认识的键，forbid 就让那一行读不出来——与
   「历史数据必须仍能读出」直接冲突。

第二条由本文件的两条测试钉住：一条证明「存量行带未知键仍然读得出来」，一条证明严格只加在请求
变体上。协议模型（Event / RunRecord）的 extra="allow" 是**有意**留的，为的是线上协议的向前兼容。
"""

from __future__ import annotations

from typing import Any

import pytest


# ------------------------------------------------------------------ 工具


def _case(client: Any, make_run: Any, **extra: Any) -> dict[str, Any]:
    client.post("/v1/ingest", json=make_run())
    payload: dict[str, Any] = {
        "name": "契约用例",
        "source_run_id": "run-1",
        "assertions": [{"type": "no_error"}],
    }
    payload.update(extra)
    response = client.post("/v1/cases", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _assert_forbidden(response: Any, key: str) -> None:
    """422 且**指出是哪个键**。只判状态码不够：说不出键名的 422 帮不到调用方。"""

    assert response.status_code == 422, f"期望 422，实际 {response.status_code}：{response.text}"
    assert key in response.text, f"422 里没有指出是哪个键：{response.text}"


# ------------------------------------------------------------------ 直接回放入口


@pytest.mark.parametrize("endpoint", ["replay", "replay/estimate"])
def test_a_replay_request_with_a_misspelled_field_is_rejected(
    client: Any, make_run: Any, endpoint: str
) -> None:
    """回放与它的预估共用同一个请求模型，因此两处都要挡住。"""

    client.post("/v1/ingest", json=make_run())
    payload = {"from_seq": 1, "systemPrompt": "caller-intended"}
    _assert_forbidden(client.post(f"/v1/runs/run-1/{endpoint}", json=payload), "systemPrompt")


def test_a_valid_replay_body_is_not_rejected_by_the_new_contract(
    client: Any, make_run: Any
) -> None:
    """收口不能变成「什么都拒」。合法请求必须通过校验，走到它本该走的判断上。

    这条用的是没注册可重建 Agent 的库：因此合法请求得到的是 409（服务端回放不了），而不是
    422（请求本身不成立）。两者分工明确，正是要区分的两件事。
    """

    client.post("/v1/ingest", json=make_run())
    response = client.post(
        "/v1/runs/run-1/replay",
        json={"from_seq": 1, "model": "m", "system_prompt": "P", "preset": "regress"},
    )
    assert response.status_code == 409, response.text


# ------------------------------------------------------------------ 批量条件


@pytest.mark.parametrize("endpoint", ["/v1/suites", "/v1/suites/estimate"])
def test_a_suite_condition_with_a_misspelled_field_is_rejected(
    client: Any, make_run: Any, endpoint: str
) -> None:
    """conditions 里的字段拼错必须 422，不能变成「沿用用例自身」那一列。

    这条路径证明「缺 case_ids 会 400」覆盖不了它：case_ids 是有效的，坏的是条件本身。
    """

    created = _case(client, make_run)
    response = client.post(
        endpoint, json={"case_ids": [created["id"]], "conditions": [{"modle": "m"}]}
    )
    _assert_forbidden(response, "modle")


def test_a_valid_suite_submission_still_goes_through(client: Any, make_run: Any) -> None:
    """同一端点上的合法请求照旧成立（条件被真的读进去，而不是被拒）。"""

    created = _case(client, make_run)
    response = client.post(
        "/v1/suites", json={"case_ids": [created["id"]], "conditions": [{"model": "m"}]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["conditions"] == [{"prompt": None, "model": "m"}]


def test_a_suite_request_with_a_misspelled_toplevel_field_is_rejected(
    client: Any, make_run: Any
) -> None:
    """顶层字段同理：case_id 少个 s 会让这次提交选错集合（或变成没有目标）。"""

    created = _case(client, make_run)
    _assert_forbidden(client.post("/v1/suites", json={"case_id": [created["id"]]}), "case_id")


# ------------------------------------------------------------------ 嵌套：预算与策略


def test_a_nested_budget_with_a_misspelled_field_is_rejected(
    client: Any, make_run: Any
) -> None:
    """max_model_call 少个 s：以前是「想限制花费，结果变成不设上限」。"""

    created = _case(client, make_run)
    response = client.post(
        f"/v1/cases/{created['id']}/run", json={"budget": {"max_model_call": 1}}
    )
    _assert_forbidden(response, "max_model_call")
    # 批量那一侧用的是同一个预算模型，同样要挡住。
    _assert_forbidden(
        client.post(
            "/v1/suites",
            json={"case_ids": [created["id"]], "budget": {"max_model_call": 1}},
        ),
        "max_model_call",
    )


def test_a_nested_policy_with_a_misspelled_field_is_rejected(
    client: Any, make_run: Any
) -> None:
    """defualt 少个 i：以前是「想放行真实副作用，实际什么都没变」。三个入口都要挡。"""

    created = _case(client, make_run)
    _assert_forbidden(
        client.post(
            "/v1/cases",
            json={
                "name": "策略拼错",
                "source_run_id": "run-1",
                "policy": {"defualt": "live"},
            },
        ),
        "defualt",
    )
    _assert_forbidden(
        client.patch(f"/v1/cases/{created['id']}", json={"policy": {"defualt": "live"}}),
        "defualt",
    )
    _assert_forbidden(
        client.post("/v1/runs/run-1/replay", json={"from_seq": 1, "policy": {"defualt": "live"}}),
        "defualt",
    )


def test_a_rejected_request_leaves_no_trace(client: Any, make_run: Any) -> None:
    """被拒的请求不该**跑起来**，也不该留下前提：一次 422 只是一次 422。"""

    created = _case(client, make_run)
    before = int(client.get("/v1/runs?limit=1").json()["total"])

    _assert_forbidden(
        client.post(f"/v1/cases/{created['id']}/run", json={"budget": {"max_model_call": 1}}),
        "max_model_call",
    )
    _assert_forbidden(
        client.post(f"/v1/cases/{created['id']}/run", json={"fromSeq": 1}), "fromSeq"
    )

    assert int(client.get("/v1/runs?limit=1").json()["total"]) == before
    after = client.get(f"/v1/cases/{created['id']}").json()
    assert after["last_definition_overrides"] is None
    assert after["last_definition_digest"] is None


# ------------------------------------------------------------------ 边界没有被收过头


def test_the_sdk_models_stay_lenient_so_stored_rows_stay_readable(
    client: Any, make_run: Any
) -> None:
    """**有意**不收 SDK 模型：存量行里多一个键，也必须读得出来。

    这是机制选择的守护。若有人图省事给 SDK 的 EffectPolicy 加上 extra="forbid"，这条会红：
    那一行是历史数据（未来版本写的、或手工改的），读不出来就等于回了「历史数据必须仍能读出」
    这句话的反面。
    """

    from agent_flight_recorder.models import EffectPolicy, Event, RunRecord

    from afr_server.db import session_scope
    from afr_server.tables import CaseTable

    client.post("/v1/ingest", json=make_run())
    stored_policy = {
        "default": "recorded",
        "by_kind": {},
        "by_seq": {},
        "allow_side_effect_execution": False,
        # 本版本不认识的键：它**不能**让这一行读不出来。
        "future_knob": "x",
    }
    with session_scope() as session:
        session.add(
            CaseTable(
                id="future-row",
                name="未来版本写下的行",
                description="",
                source_run_id="run-1",
                from_seq=1,
                assertions=[{"type": "no_error"}],
                labels={},
                effect_policy={
                    "preset": None,
                    "policy": stored_policy,
                    "model": None,
                    "system_prompt": None,
                },
                last_results=[],
            )
        )

    response = client.get("/v1/cases/future-row")
    assert response.status_code == 200, response.text
    assert response.json()["policy"]["default"] == "recorded"

    # 直接对模型说清楚这条选择：宽松是刻意的，收紧会让上面那行读不出来。
    assert EffectPolicy.model_validate(stored_policy).default.value == "recorded"
    assert Event.model_config.get("extra") == "allow"
    assert RunRecord.model_config.get("extra") == "allow"


def test_the_request_side_models_are_the_strict_ones() -> None:
    """严格只加在**请求**变体上：SDK 模型仍然宽松，两者是不同的类型。"""

    from agent_flight_recorder.models import EffectPolicy, ReplayBudget

    from afr_server.schemas import (
        BudgetRequest,
        CaseCreateRequest,
        CaseRunRequest,
        CaseUpdateRequest,
        PolicyRequest,
        ReplayRequest,
        SuiteCondition,
        SuiteSubmitRequest,
    )

    for request_model in (
        BudgetRequest,
        CaseCreateRequest,
        CaseRunRequest,
        CaseUpdateRequest,
        PolicyRequest,
        ReplayRequest,
        SuiteCondition,
        SuiteSubmitRequest,
    ):
        assert request_model.model_config.get("extra") == "forbid", request_model.__name__

    assert issubclass(BudgetRequest, ReplayBudget)
    assert issubclass(PolicyRequest, EffectPolicy)
    assert ReplayBudget.model_config.get("extra") != "forbid"
    assert EffectPolicy.model_config.get("extra") != "forbid"


# ------------------------------------------------------------------ 同类检查：其它公开入口


def test_diff_still_rejects_a_missing_run_id(client: Any, make_run: Any) -> None:
    """diff 的两个参数是必填：拼错名字只会漏掉必填项，因此它本来就是 422，不会静默降级。"""

    client.post("/v1/ingest", json=make_run())
    assert client.get("/v1/diff?a=run-1&bb=run-1").status_code == 422


def test_read_filters_are_a_known_boundary_not_an_execution_premise(
    client: Any, make_run: Any
) -> None:
    """读过滤（?status= 之类）拼错会被忽略——**这条不在本轮收口范围**，但它不是执行前提。

    分界是这样划的：执行前提决定「跑什么、怎么跑」，拼错会让**别的东西跑起来**（或用别的前提跑），
    代价不对称、事后也无从分辨，因此必须在契约上挡住；读过滤拼错只会让调用方看到**全量**而不是
    筛过的子集，列表本身仍然是真值，事后一眼能看出来。

    这里把现状钉住，是让它成为一条**写明的**边界，而不是一个没人知道的漏洞。要收口的话机制与
    上面同源（在依赖里对照查询参数白名单），但那是另一件事，得由它自己的理由驱动。
    """

    client.post("/v1/ingest", json=make_run())
    response = client.get("/v1/runs?limit=5&staus=succeeded")
    assert response.status_code == 200
    # 没有按那个拼错的字段过滤：返回的是全量（这条钉住的是现状，不是「正确」）。
    assert response.json()["total"] == 1

