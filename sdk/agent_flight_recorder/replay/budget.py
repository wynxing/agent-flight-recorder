"""回放预算：事先可预估、执行中硬停、事后如实记账。

预算这条能力由三件事组成，缺任何一件它都是假的：

* **事先可预估** —— estimate_replay_budget 只看父 Run 的录制与回放计划，不调用任何
  模型。父 Run 缺 token、或模型不在价格表内时，如实返回「无法预估」
  （cost_usd = None）而不是编一个数字。
* **执行中硬停** —— BudgetLedger 在**步边界**判定；达到任一上限时引擎停止，不再发起
  新的模型调用。已经发出的那次调用允许完成并如实记账（不做预测性中断）。
* **事后如实记账** —— BudgetUsage 给出「已用 / 上限 / 是否触顶」。成本未知时是 None
  （未知），不是 0：沿用 cost.py 的「不猜」立场。

上限只约束**真实发生的模型调用**。复现模式读的是录制结果，本来就不花模型钱，因此它
既不消耗次数也不消耗成本，也就不会被这里的任何一条判定误伤（见
docs/replay-semantics.md 第 9 节）。
"""

from __future__ import annotations

from typing import Any, Sequence

from pydantic import BaseModel

from ..cost import estimate_cost
from ..models import EffectMode, Event, EventType, ReplayBudget, RunRecord
from .fork import behavioral_steps

#: 触顶的那一维。取值是稳定的机器值，控制台与元数据都用它，不再各自造句。
STOPPED_BY_MODEL_CALLS = "model_calls"
STOPPED_BY_COST = "cost"


class BudgetUsage(BaseModel):
    """一次回放结束时的预算记账：已用 / 上限 / 是否触顶。"""

    #: 声明的上限；None 表示这一维不参与判定。
    max_cost_usd: float | None = None
    max_model_calls: int | None = None
    #: 实际发生的真实模型调用次数。
    model_calls_used: int = 0
    #: 实际发生的成本（估算）。None 表示**未知**（模型不在价格表内或没有 token 记录），
    #: 不是 0。
    cost_used_usd: float | None = None
    cost_is_estimate: bool = True
    #: 是否触顶：这次回放**因触顶而停止**。账正好用满但回放正常跑完时是 False。
    exceeded: bool = False
    #: 触顶的那一维（model_calls / cost）；没触顶时为 None。
    stopped_by: str | None = None
    #: 是否存在无法定价的真实调用（成本记成「未知」的原因）。
    cost_unknown: bool = False
    #: 给人看的一句说明。
    detail: str = ""


class ReplayEstimate(BaseModel):
    """一次回放的预估：预计产生多少次真实模型调用、大概花多少。

    cost_usd 为 None 就是「无法预估」。这不是失败，而是如实：父 Run 的 token 记录或
    价格表缺一块时，任何具体数字都是编的。
    """

    parent_run_id: str
    from_seq: int
    #: 预计真实执行的模型调用次数（不含读取录制结果的复现步骤）。
    model_calls: int = 0
    #: 预计成本（USD）。None = 无法预估。
    cost_usd: float | None = None
    #: 永远是估算：价格表是本地快照，回归模式下新上下文与父 Run 也不完全相同。
    cost_is_estimate: bool = True
    detail: str = ""


class BudgetLedger:
    """执行期的预算账本。只记真实发生的模型调用，不记复现出来的步骤。"""

    def __init__(self, budget: ReplayBudget | None = None) -> None:
        self.budget = budget or ReplayBudget()
        self.model_calls_used = 0
        self.stopped_by: str | None = None
        self._cost = 0.0
        self._cost_unknown = False

    def note_model_call(self, cost_usd: float | None) -> None:
        """记一次**已经完成**的真实模型调用。成本未知时按「未知」记，不按 0 记。"""

        self.model_calls_used += 1
        if cost_usd is None:
            # 只要有一次调用无法定价，总量就是「未知」：把未知当成 0 会把一次真实支出
            # 说成没花钱，这正是 cost.py 明确拒绝的做法。
            self._cost_unknown = True
            return
        self._cost += float(cost_usd)

    @property
    def cost_used_usd(self) -> float | None:
        return None if self._cost_unknown else round(self._cost, 6)

    @property
    def cost_unknown(self) -> bool:
        return self._cost_unknown

    def exceeded_by(self) -> str | None:
        """返回触顶的那一维，或 None。「达到」就算触顶，因此是 >= 而不是 >。"""

        limit = self.budget.max_model_calls
        if limit is not None and self.model_calls_used >= limit:
            return STOPPED_BY_MODEL_CALLS
        cost = self.cost_used_usd
        cap = self.budget.max_cost_usd
        if cap is not None and cost is not None and cost >= cap:
            return STOPPED_BY_COST
        return None

    def usage(self) -> BudgetUsage:
        # 「触顶」= 这次回放**因触顶而停止**，因此只看引擎真的停在哪一维。
        # 账正好用满、但后面已经没有需要真实执行的步骤时，回放是正常跑完的：
        # 那时它没有触顶（已用 / 上限两个数字依然如实给出对照）。
        stopped_by = self.stopped_by
        usage = BudgetUsage(
            max_cost_usd=self.budget.max_cost_usd,
            max_model_calls=self.budget.max_model_calls,
            model_calls_used=self.model_calls_used,
            cost_used_usd=self.cost_used_usd,
            cost_unknown=self._cost_unknown,
            exceeded=stopped_by is not None,
            stopped_by=stopped_by,
        )
        usage.detail = usage_detail(usage)
        return usage

    def remaining(self) -> ReplayBudget:
        """这份账本还剩下多少额度（两个维度各自独立）。

        给「把整批剩下的额度分给下一个格子」用：批量套件在启动每一格之前调用它，
        因此格子最多只可能花掉真正剩下的那部分。未声明的维度仍然是 None（不参与判定）。

        成本未知时**不设成本额度**，而不是把未知当成 0：未知不是「已经花光了」，
        给下一个格子一个凭空的 0 上限既是编造，也会把本该继续跑的格子误伤掉。
        调用次数这一维不受成本未知的影响，照常按计数算剩余。
        """

        calls = None
        if self.budget.max_model_calls is not None:
            calls = max(0, self.budget.max_model_calls - self.model_calls_used)
        cost = None
        if self.budget.max_cost_usd is not None and self.cost_used_usd is not None:
            cost = max(0.0, round(self.budget.max_cost_usd - self.cost_used_usd, 6))
        return ReplayBudget(max_cost_usd=cost, max_model_calls=calls)


def usage_detail(usage: BudgetUsage) -> str:
    """把记账翻成一句人话。未知就说未知，不说 0。"""

    calls = f"{usage.model_calls_used} 次真实模型调用"
    if usage.cost_used_usd is None:
        money = (
            "成本未知（有真实调用无法按本地价格表定价）"
            if usage.cost_unknown
            else "成本未产生"
        )
    elif usage.cost_used_usd == 0:
        money = "没有产生成本"
    else:
        money = f"已用成本约 ${usage.cost_used_usd:.6f}（估算）"

    if usage.stopped_by == STOPPED_BY_MODEL_CALLS:
        return (
            f"因达到模型调用上限而停止：已用 {calls}（上限 {usage.max_model_calls} 次）。"
            f"{money}。"
        )
    if usage.stopped_by == STOPPED_BY_COST:
        return f"因达到成本上限而停止：{money}（上限 ${usage.max_cost_usd}）。"
    return f"没有因预算停止：已用 {calls}。{money}。"


def estimate_replay_budget(
    plan: Any,
    parent_run: RunRecord,
    parent_events: Sequence[Event],
) -> ReplayEstimate:
    """预估一次回放的成本与调用量。纯计算，不产生任何真实调用。

    与引擎共用同一套策略解析：分叉点之前的步骤一律按录制结果复现，因此不计入预估；
    只有解析成 live 的模型调用才会真的花钱。
    """

    ordered = sorted(parent_events, key=lambda event: event.seq)
    live_calls: list[Event] = []
    for step in behavioral_steps(ordered):
        if step.type is not EventType.MODEL_CALL or step.seq < plan.from_seq:
            continue
        if plan.policy.resolve_mode(seq=step.seq, kind=step.type.value) is not EffectMode.LIVE:
            continue
        live_calls.append(step)

    if not live_calls:
        return ReplayEstimate(
            parent_run_id=plan.parent_run_id,
            from_seq=plan.from_seq,
            model_calls=0,
            cost_usd=0.0,
            detail="这次计划里没有任何一步会真实调用模型（全部使用录制结果），预计成本为 0。",
        )

    model = plan.overrides.model or parent_run.model
    total = 0.0
    unknown: list[str] = []
    for step in live_calls:
        tokens = step.tokens
        if tokens is None or (tokens.input is None and tokens.output is None):
            unknown.append(f"第 {step.seq} 步在父 Run 里没有 token 记录")
            continue
        name = model or step.name
        cost = estimate_cost(name, tokens.input, tokens.output)
        if cost is None:
            unknown.append(f"第 {step.seq} 步的模型 {name or '未标注'} 不在本地价格表内")
            continue
        total += cost

    count = len(live_calls)
    if unknown:
        return ReplayEstimate(
            parent_run_id=plan.parent_run_id,
            from_seq=plan.from_seq,
            model_calls=count,
            cost_usd=None,
            detail=(
                f"预计 {count} 次真实模型调用；成本无法预估："
                + "；".join(dict.fromkeys(unknown))
                + "。缺少的部分不会被当成 0。"
            ),
        )

    estimate = round(total, 6)
    return ReplayEstimate(
        parent_run_id=plan.parent_run_id,
        from_seq=plan.from_seq,
        model_calls=count,
        cost_usd=estimate,
        detail=(
            f"预计 {count} 次真实模型调用，估算成本约 ${estimate:.6f}（估算）。"
            "价格表是本地快照，回归模式下新上下文与父 Run 也不完全相同，因此这只是估算。"
        ),
    )


__all__ = [
    "STOPPED_BY_COST",
    "STOPPED_BY_MODEL_CALLS",
    "BudgetLedger",
    "BudgetUsage",
    "ReplayEstimate",
    "estimate_replay_budget",
    "usage_detail",
]
