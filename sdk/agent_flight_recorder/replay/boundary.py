"""回放边界：引擎与框架之间那条只认证据、不认异常类型的收尾路径。

引擎用异常把「这一次回放给不出结论」带出执行现场（ReplayExhaustedError，成因在
cause.code 上，见 engine.py 的类注释）。这条契约最初只在 LangGraph 上被验证过：
它的中间件会把异常原样抛上来，于是边界直接读 exc.cause 就够了。

第二个框架（OpenAI Agents SDK）不是这样，它有两条与 LangGraph 不同的失败通道，
两条都会让「按异常类型判定」的边界给出错答案：

* 工具里抛出的异常会被默认的 failure_error_function **转换成给模型看的文本**，
  回放于是继续往下跑，最后给出一个看似成功的结论（而不是「无法判断」）；
* 换掉失败通道、让异常真的冒出来之后，外层仍会把它**包成 UserError**，
  原始异常只剩在 __cause__ 链上，.code 在读不到的那一层；
* 模型调用里抛出的异常则是原样冒出来的（这一点与 LangGraph 一致）。

因此判定只能依赖两件与框架无关的东西：**异常链上是否带着结构化成因**，以及
ReplaySession.incomplete_reason（引擎自己记下的判定）。两者都由本模块提供，
两个适配层共用同一条收尾路径，于是「同一份录制在两个框架上回放、结论一致」不是靠
两个适配层各自写对一遍来保证的。

反向约束同样重要：这里恢复不出成因时**不猜**，不会为了凑一个码而自造字符串
（历史上有过服务端自造 replay_recording_loss 的教训，见 replay/reasons.py）。
"""

from __future__ import annotations

import json
from typing import Any

from ..models import (
    ATTR_GATE,
    ATTR_NODE,
    ATTR_REASON,
    ATTR_SEVERITY,
    SEVERITY_WARNING,
    RunStatus,
)
from ..recorder import Recorder
from .engine import (
    ReplayExhaustedError,
    ReplayPlan,
    ReplayResult,
    ReplaySession,
    StepPlan,
)
from .reasons import CAUSE_CODES, InconclusiveCode, InconclusiveReason

#: 异常链最多走这么多层。框架的包装一般只有一两层，再多就不是因果而是噪声了。
MAX_CAUSE_DEPTH = 8
NEWLINE = chr(10)


def cause_of(
    exc: BaseException, *, session: ReplaySession | None = None
) -> InconclusiveReason | None:
    """从一次失败里恢复结构化成因。恢复不出来时返回 None（不是猜一个）。

    顺序：异常链（显式 raise ... from 优先，其次隐式上下文）> 会话里已记下的判定。
    后者覆盖的是一种真实情形：引擎在抛出之前就把判定记在了会话上，而框架
    可能把异常对象整个吃掉、只留下一句话。
    """

    found = _cause_in_chain(exc)
    if found is not None:
        return found
    if session is not None:
        return session.incomplete_reason
    return None


def _cause_in_chain(exc: BaseException) -> InconclusiveReason | None:
    for attribute in ("__cause__", "__context__"):
        found = _walk_chain(exc, attribute)
        if found is not None:
            return found
    return None


def _walk_chain(exc: BaseException, attribute: str) -> InconclusiveReason | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < MAX_CAUSE_DEPTH and id(current) not in seen:
        seen.add(id(current))
        found = _cause_carried_by(current)
        if found is not None:
            return found
        current = getattr(current, attribute, None)
        depth += 1
    return None


def _cause_carried_by(exc: BaseException) -> InconclusiveReason | None:
    """这一层异常自己带着的成因（引擎的异常对象，或任何带 code 的同形载体）。"""

    carried = getattr(exc, "cause", None)
    if isinstance(carried, InconclusiveReason):
        return carried
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in CAUSE_CODES:
        detail = getattr(exc, "detail", None)
        return InconclusiveReason.from_code(code, detail if isinstance(detail, str) else str(exc))
    return None


def finish_failed_replay(
    *,
    session: ReplaySession,
    recorder: Recorder,
    exc: BaseException,
) -> ReplayResult:
    """一次没能跑完的回放的统一收尾。两个适配层的失败出口都走这里。

    语义（与接入第二个框架之前逐字一致，只是「成因能不能被恢复」不再取决于框架）：

    * 能恢复出成因：complete=False + 结构化成因；因预算触顶的用 aborted
      （没跑完，不是失败），其余成因保持 failed；
    * 恢复不出成因：failed，且不凭空造一个成因，complete 维持既有取值。
    """

    cause = cause_of(exc, session=session)
    if cause is None:
        recorder.record_error(exc)
        recorder.finish(status=RunStatus.FAILED)
        return session.to_result(
            run_id=recorder.run_id,
            status=RunStatus.FAILED.value,
            error=f"{type(exc).__name__}: {exc}",
        )
    recorder.record_error(exc, reason="replay_exhausted")
    # 因预算停止不是「失败」，而是「没跑完」：Run 状态用 aborted（与 pi 侧对预算触顶
    # 的处置一致），结论由成因码 budget_exceeded 表达，用例判定落到 inconclusive。
    status = (
        RunStatus.ABORTED
        if cause.code == InconclusiveCode.BUDGET_EXCEEDED.value
        else RunStatus.FAILED
    )
    recorder.finish(status=status)
    return session.to_result(
        run_id=recorder.run_id,
        status=status.value,
        error=str(exc),
        complete=False,
        cause=cause,
    )


def apply_plan_to_recorder(plan: ReplayPlan, recorder: Recorder) -> Recorder:
    """把回放的父子关系写进 Run 头。

    回放是新的 Run，不是对旧 Run 的修改；父子关系靠 parent_run_id 表达。
    这一段只写 Run 头字段，与框架无关，因此放在边界层由两个适配层共用，
    避免第二个适配器再抄一份。
    """

    recorder.run.parent_run_id = plan.parent_run_id
    recorder.run.replay_from_seq = plan.from_seq
    recorder.run.effect_policy = plan.policy
    if plan.agent_version:
        recorder.run.agent_version = plan.agent_version
    if plan.prompt_version:
        recorder.run.prompt_version = plan.prompt_version
    return recorder


def step_attributes(plan: StepPlan, *, node: str = "replay") -> dict[str, Any]:
    """一步回放要写进事件上的标注。框架无关：只说这一步是被怎么处置的。"""

    attributes: dict[str, Any] = {ATTR_NODE: node}
    if plan.reason:
        attributes[ATTR_REASON] = plan.reason
    if plan.downgraded:
        # 被闸门降级过的步骤，控制台要能一眼看出来，而不是只看 effect_source。
        attributes[ATTR_GATE] = "side_effect_gate"
        attributes[ATTR_SEVERITY] = SEVERITY_WARNING
    return attributes


def dry_run_text(name: str, args: Any) -> str:
    """dry_run 步骤给模型的合成结果：说清「本应做什么」，而不是假装做过了。"""

    head = f"[afr dry-run] 未真实执行 {name}。回放策略拦截了这一步的副作用。"
    body = f"本应执行的操作参数: {json.dumps(args, ensure_ascii=False, default=str)}"
    return head + NEWLINE + body


def missing_recorded_result_cause(name: str, args: Any) -> InconclusiveReason:
    """复现时找不到匹配的录制结果：成因码稳定，散文只进 detail。

    两个框架的工具层都从这一条路径给出同一个码；差异只在各自的工具对象长什么样。
    """

    detail = NEWLINE.join(
        [
            f"工具 {name} 的这次调用在父 Run 里没有匹配的录制结果，回放已偏离原始轨迹。",
            f"本次调用参数: {json.dumps(args, ensure_ascii=False, default=str)}",
            "如需继续，请改用回归模式让工具真实执行，或检查 Agent 行为为何改变。",
        ]
    )
    return InconclusiveReason.from_code(InconclusiveCode.MISSING_RECORDED_RESPONSE.value, detail)


__all__ = [
    "MAX_CAUSE_DEPTH",
    "ReplayExhaustedError",
    "apply_plan_to_recorder",
    "cause_of",
    "dry_run_text",
    "finish_failed_replay",
    "missing_recorded_result_cause",
    "step_attributes",
]
