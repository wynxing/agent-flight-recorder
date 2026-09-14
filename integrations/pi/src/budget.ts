/**
 * 批量预算：与 Python 侧 `ReplayBudget` / `BudgetLedger` 同一套语义。
 *
 * 这里刻意不引入第二套字段名，也不引入第二套判定：
 *
 * * 两个维度与 Python 同名：`max_model_calls` / `max_cost_usd`；没声明的一维是
 *   `null`，表示「这一维不参与判定」，不是「上限为 0」。
 * * **执行中在步边界硬停**：发出去之前先看账，达到上限就不再发起新的模型调用；
 *   已经发出的那次调用允许完成并如实记账（不做预测性中断）。
 * * **成本未知不是 0**：只要有一次真实调用无法定价，成本就是未知，这一维不再参与
 *   判定（`null`），而不是把未知当成没花钱。
 * * `stopped_by ∈ {model_calls, cost}`，触顶的取值与控制台、Python 侧是同一组机器值。
 *
 * 对齐清单见 docs/replay-semantics.md 第 9 节。
 */

/** 声明的上限。两个维度各自独立；`null` = 这一维不参与判定。 */
export interface BudgetSpec {
  max_cost_usd?: number | null;
  max_model_calls?: number | null;
}

/** 触顶的那一维。取值是稳定的机器值，与 Python 侧逐字一致。 */
export const STOPPED_BY_MODEL_CALLS = 'model_calls';
export const STOPPED_BY_COST = 'cost';

/** 只有真的声明了至少一个上限，才有记账对象。 */
export function isDeclared(spec: BudgetSpec | null | undefined): boolean {
  return !!spec && (spec.max_cost_usd != null || spec.max_model_calls != null);
}

export interface BudgetUsage {
  max_cost_usd: number | null;
  max_model_calls: number | null;
  model_calls_used: number;
  cost_used_usd: number | null;
  cost_is_estimate: true;
  exceeded: boolean;
  stopped_by: string | null;
  cost_unknown: boolean;
  detail: string;
}

/**
 * 执行期的账本。只记**真实发生的模型调用**；读取录制结果的步骤既不花成本也不占次数。
 */
export class BudgetLedger {
  readonly spec: BudgetSpec;
  modelCallsUsed = 0;
  stoppedBy: string | null = null;
  private cost = 0;
  private costUnknown = false;

  constructor(spec: BudgetSpec | null | undefined) {
    this.spec = spec ?? {};
  }

  /** 记一次**已经完成**的真实模型调用。成本未知时按未知记，不按 0 记。 */
  noteModelCall(costUsd: number | null | undefined): void {
    this.modelCallsUsed += 1;
    if (costUsd == null || !Number.isFinite(costUsd)) {
      // 只要有一次调用无法定价，总量就是「未知」：把未知当成 0 会把一次真实支出说成
      // 没花钱，这正是 cost.py 明确拒绝的做法。
      this.costUnknown = true;
      return;
    }
    this.cost += costUsd;
  }

  get costUsedUsd(): number | null {
    return this.costUnknown ? null : round6(this.cost);
  }

  /**
   * 返回触顶的那一维，或 null。「达到」就算触顶，因此是 >= 而不是 >；
   * 调用次数这一维先判（与 Python 侧同一顺序）。成本未知时成本维不参与判定。
   */
  exceededBy(): string | null {
    const limit = this.spec.max_model_calls;
    if (limit != null && this.modelCallsUsed >= limit) return STOPPED_BY_MODEL_CALLS;
    const cost = this.costUsedUsd;
    const cap = this.spec.max_cost_usd;
    if (cap != null && cost != null && cost >= cap) return STOPPED_BY_COST;
    return null;
  }

  /**
   * 这份账本还剩下多少额度（两个维度各自独立）。给「把整批剩下的额度分给下一格」用。
   * 成本未知时**不设成本额度**，而不是把未知当成 0：给下一个格子一个凭空的 0 上限
   * 既是编造，也会把本该继续跑的格子误伤掉。
   */
  remaining(): BudgetSpec {
    const calls = this.spec.max_model_calls == null
      ? null
      : Math.max(0, this.spec.max_model_calls - this.modelCallsUsed);
    const cost = this.spec.max_cost_usd != null && this.costUsedUsd != null
      ? Math.max(0, round6(this.spec.max_cost_usd - this.costUsedUsd))
      : null;
    return { max_cost_usd: cost, max_model_calls: calls };
  }

  /** 已用 / 上限 / 是否触顶。触顶 = 这次执行**因触顶而停止**，只看真的停在哪一维。 */
  usage(): BudgetUsage {
    const usage: BudgetUsage = {
      max_cost_usd: this.spec.max_cost_usd ?? null,
      max_model_calls: this.spec.max_model_calls ?? null,
      model_calls_used: this.modelCallsUsed,
      cost_used_usd: this.costUsedUsd,
      cost_is_estimate: true,
      exceeded: this.stoppedBy != null,
      stopped_by: this.stoppedBy,
      cost_unknown: this.costUnknown,
      detail: '',
    };
    usage.detail = usageDetail(usage);
    return usage;
  }
}

const round6 = (value: number) => Math.round(value * 1e6) / 1e6;

/** 把记账翻成一句人话。未知就说未知，不说 0。 */
export function usageDetail(usage: BudgetUsage): string {
  const calls = `${usage.model_calls_used} 次真实模型调用`;
  let money: string;
  if (usage.cost_used_usd == null) {
    money = usage.cost_unknown
      ? '成本未知（有真实调用无法按本地价格表定价）'
      : '成本未产生';
  } else if (usage.cost_used_usd === 0) {
    money = '没有产生成本';
  } else {
    money = `已用成本约 $${usage.cost_used_usd.toFixed(6)}（估算）`;
  }
  if (usage.stopped_by === STOPPED_BY_MODEL_CALLS) {
    return `因达到模型调用上限而停止：已用 ${calls}（上限 ${usage.max_model_calls} 次）。${money}。`;
  }
  if (usage.stopped_by === STOPPED_BY_COST) {
    return `因达到成本上限而停止：${money}（上限 $${usage.max_cost_usd}）。`;
  }
  return `没有因预算停止：已用 ${calls}。${money}。`;
}

/**
 * 触顶时的说明：说清停在哪一维、已用多少，并给出下一步。与 Python 侧
 * `ReplaySession.budget_stop_detail` 同义（两侧都只陈述发生了什么）。
 */
export function stopDetail(usage: BudgetUsage, stoppedBy: string): string {
  let head: string;
  if (stoppedBy === STOPPED_BY_MODEL_CALLS) {
    head = `已达到本次回放的模型调用上限：已用 ${usage.model_calls_used} 次（上限 ${usage.max_model_calls} 次）。`;
  } else if (stoppedBy === STOPPED_BY_COST) {
    head = `已达到本次回放的成本上限：已用约 $${(usage.cost_used_usd ?? 0).toFixed(6)}（上限 $${usage.max_cost_usd}，估算）。`;
  } else {
    head = '已达到本次回放的预算上限。';
  }
  return (
    head +
    '回放已在步边界停止，没有继续发起新的模型调用；已经发出的那次调用已如实记账。' +
    '提高上限或缩小回放范围后可以重跑。'
  );
}

/** 未启动：格子从未被执行过，因此它没有任何结论。 */
export const NOT_STARTED = 'not_started';
export const NOT_STARTED_REASON = '因批次预算用尽';
export const NOT_STARTED_LABEL = `未启动（${NOT_STARTED_REASON}）`;

/**
 * 整批记账的说明：单次那一句 + 「多少格没跑、为什么」。
 * 触顶时这里不会出现「跑完了」「全部通过」「拿不到结论」这类措辞：没跑的格子既不是
 * 通过，也不是「跑过了但拿不到结论」——它们只是没跑。
 */
export function batchUsageDetail(usage: BudgetUsage, notStarted: number): string {
  const detail = usageDetail(usage);
  if (!notStarted) return detail;
  return (
    detail +
    `整批有 ${notStarted} 个格子${NOT_STARTED_LABEL}，` +
    '它们从未执行，因此没有任何结论，也不计入未通过或无法判断。'
  );
}

/**
 * 批量调度的一步：账已经到点时不再启动新格子（返回 null），并**先把这一维写进账**，
 * 再让格子变成终态——顺序与 Python 侧一致（先记账，批次才不会读到一个「已结束、
 * 但账停在上一格」的批次）。
 */
export function beginCell(ledger: BudgetLedger): BudgetSpec | null {
  const stoppedBy = ledger.exceededBy();
  if (stoppedBy == null) return ledger.remaining();
  ledger.stoppedBy = stoppedBy;
  return null;
}

/** 一次录制里真实发生的一次模型调用留下的用量。 */
export interface RecordedCall {
  tokens: number | null;
  /** 这次调用的成本；null = 无法定价（未知，不是 0）。 */
  costUsd: number | null;
}

export interface EstimateInput {
  /** 每个任务一次录制里真实发生的模型调用。 */
  task: string;
  calls: RecordedCall[];
}

export interface BatchEstimate {
  cells: number;
  model_calls: number;
  cost_usd: number | null;
  cost_is_estimate: true;
  unknown: string[];
  detail: string;
}

/**
 * 预估整批要花多少：按「父录制里真实发生的调用次数与成本」× 计划跑多少格。
 *
 * 与 Python 侧的预估同一立场：只看会真实执行的调用，缺数据就如实说「无法预估」
 * （cost_usd = null），绝不拿已知部分凑一个确定数字。**父录制里的调用次数是下界**：
 * 回归时模型可能比录制那次多走几步，预估里不包含这部分（这一条必须在说明里写明）。
 */
export function estimateBatch(records: EstimateInput[], cellsPerTask: number): BatchEstimate {
  const cells = records.length * cellsPerTask;
  let calls = 0;
  let cost = 0;
  const unknown: string[] = [];
  for (const record of records) {
    calls += record.calls.length * cellsPerTask;
    for (const call of record.calls) {
      if (call.costUsd == null || !Number.isFinite(call.costUsd)) {
        unknown.push(`${record.task}：有一次已完成的模型调用无法按本地价格表定价`);
        continue;
      }
      cost += call.costUsd * cellsPerTask;
    }
  }
  const costUsd = unknown.length ? null : round6(cost);
  const tail =
    '这是估算：它按父录制里真实发生的调用次数与成本折算，**不包含**回归时超出父录制尾部的新增步骤；' +
    '价格表是本地快照。';
  return {
    cells,
    model_calls: calls,
    cost_usd: costUsd,
    cost_is_estimate: true,
    unknown,
    detail: costUsd == null
      ? `预计 ${calls} 次真实模型调用；成本无法预估：${[...new Set(unknown)].join('；')}。缺的那部分不会被当成 0。`
      : `预计 ${calls} 次真实模型调用，估算成本约 $${costUsd.toFixed(6)}（估算）。${tail}`,
  };
}

export function budgetSpecFromOptions(options: { maxCostUsd?: string | number | null; maxModels?: string | number | null }): BudgetSpec | null {
  const cost = numberOrNull(options.maxCostUsd, 'budget-cost-usd');
  const calls = intOrNull(options.maxModels, 'budget-models');
  const spec: BudgetSpec = { max_cost_usd: cost, max_model_calls: calls };
  return isDeclared(spec) ? spec : null;
}

function numberOrNull(value: string | number | null | undefined, name: string): number | null {
  if (value == null || value === '') return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error(`Invalid --${name}`);
  return parsed;
}

function intOrNull(value: string | number | null | undefined, name: string): number | null {
  if (value == null || value === '') return null;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) throw new Error(`Invalid --${name}`);
  return parsed;
}
