/**
 * 批量汇总里「这句话能不能说」的判定，集中在这里。
 *
 * 这个文件刻意没有任何 import，因此可以被直接执行来做单测
 * （见 server/tests/test_suites.py 的跨语言契约测试）。文案是这一层唯一的产物：
 * 把判定留在组件模板里，就没法钉住它了。
 *
 * 两条硬约束：
 *
 * 1. **「还没跑」不等于「拿不到结论」**。未完成的格子从未执行过、因此没有任何结论，
 *    对它下「拿不到」这个断言没有证据支撑——那本该是只有执行过才成立的说法。
 *    因此只有整批跑完，才允许说出「N 条中 M 条拿不到结论」。
 * 2. **每个短语只指向一个数**。同一张卡片上：
 *    已完成 = completed，未完成 = unfinished，拿不到结论 = undecided，
 *    通过 / 未通过 / 无法判断 / 执行出错 各自等于同名的计数。
 * 3. **「没跑」要说清为什么**。未完成里有一类是「未启动（因批次预算用尽）」：它不是
 *    「还没轮到」，而是「不会再轮到」。这个数单独给出（not_started），因为用户看到它
 *    该做的是调上限，而不是继续等。
 *
 * 这与本项目「结论只说证据支持的事」的立场同源，也与第 3 轮区分 inconclusive 与 failed 同源。
 */

/** 一个条件的进度。结构类型，避免这个文件依赖任何运行时代码。 */
export interface ConditionProgress {
  total: number
  completed: number
  determinable: number
  undecided: number
  unfinished: number
  /** 未完成里「因批次预算用尽而未启动」的那部分（unfinished 的子集）。 */
  not_started?: number
  determinable_rate?: number | null
}

/**
 * 「未启动」的原因。整批预算用尽才会产生这个状态，因此这句话只有这一个来源：
 * 服务端 suites.NOT_STARTED_REASON 与这里必须逐字一致（由跨语言契约测试钉住，
 * 见 server/tests/test_suite_budget.py）。
 */
export const NOT_STARTED_REASON = '因批次预算用尽'

/** 未启动格子的完整说法：状态 + 原因。看到它就该知道该去调什么。 */
export function notStartedLabel(): string {
  return '未启动（' + NOT_STARTED_REASON + '）'
}

/**
 * 「未完成」这一格的构成说明。没有未启动的格子时是空串（那句话说不出任何东西）。
 *
 * 两个数分开给：未完成 = 还没轮到 + 因整批预算用尽而未启动。合成一个数，用户就分不清
 * 该继续等，还是该去调上限。
 */
export function unfinishedBreakdown(progress: {
  unfinished: number
  not_started?: number
}): string {
  const count = progress.not_started
  if (!count || count <= 0) return ''
  return '，其中 ' + count + ' 条' + notStartedLabel()
}

/**
 * 一个条件现在能说出的一句话。
 *
 * * 还有格子没完成时：只陈述「已完成 / 未完成」，不对结论下任何断言；
 * * 整批完成时：才说「N 条中 M 条拿不到结论」——此时 M 是被真的判定为无法判断的条数，
 *   这句话才有完整证据。
 */
export function conditionVerdict(group: ConditionProgress): string {
  if (group.unfinished > 0) {
    return (
      '共 ' + group.total + ' 条：已完成 ' + group.completed +
      '、未完成 ' + group.unfinished + unfinishedBreakdown(group)
    )
  }
  return group.total + ' 条中 ' + group.undecided + ' 条拿不到结论'
}

/**
 * 可判断率一行。样本量（分子/分母）永远与百分比一起出现，两者不可拆开；
 * 比率本身用服务端算好的值，控制台不另立一份定义。
 */
export function determinableRateText(group: ConditionProgress): string {
  const rate = group.determinable_rate
  if (rate === null || rate === undefined) return '-'
  return Math.round(rate * 100) + '%（' + group.determinable + '/' + group.total + '）'
}

/**
 * 下钻入口的措辞。用与汇总句**同一个短语**（拿不到结论）、同一个数（有成因的格子数，
 * 也就是 undecided）：一张卡片上不允许同一个短语指向两个数。
 */
export function causeDrillLabel(count: number): string {
  return '展开 ' + count + ' 条拿不到结论的成因'
}

/** 批次的生命周期，用于标题行的角标。 */
export interface BatchLifecycle {
  status: string
  /** 整批是否因预算触顶而停止。 */
  exceeded?: boolean | null
  /** 因整批预算用尽而没跑的格子数。 */
  not_started?: number | null
}

/**
 * 批次角标的一句话。「已完成」只能指向一个数（completed），因此**触顶而停止的批次不许
 * 显示它**：那种批次里还有格子从未跑过，「已完成」与紧邻的「已完成 X / Y」会变成同词两义，
 * 也等于把「没跑完」说成「跑完了」——那正是这一轮反复强调不许出现的措辞。
 *
 * 触顶（或有格子未启动）时说的是「已停止」：它陈述的是一个事实（不会再有格子启动了），
 * 既没声称跑完，也没声称通过。
 */
export function batchLifecycleLabel(batch: BatchLifecycle): string {
  if (batch.status === 'running') return '进行中'
  if (batch.exceeded || (batch.not_started ?? 0) > 0) return '已停止'
  return '已完成'
}

/**
 * 条件的可读标签：只写这个条件**实际覆盖了**什么。
 *
 * 不确定的维度要如实说成「沿用用例自身」，而不是替它起一个名字（例如「默认模型」）：
 * 没有覆盖时，每个格子用的是各自用例自己的模型，前端与服务端都担保不了那个默认值。
 *
 * 服务端 suites.condition_label() 有同一套规则；两者由跨语言契约测试钉住一致
 * （server/tests/test_suites.py::test_console_and_server_agree_on_the_condition_label）。
 */
export function conditionLabel(condition: ConditionSpec): string {
  const prompt = condition.prompt || ''
  const model = condition.model || ''
  if (!prompt && !model) return '沿用用例自身条件'
  return (prompt || '沿用用例自身 Prompt') + ' · ' + (model || '沿用用例自身模型')
}

/** 条件里的两个维度；缺省表示这次没有覆盖它。 */
export interface ConditionSpec {
  prompt?: string | null
  model?: string | null
}

/**
 * 一个用例集版本（服务端 case_versions.py 的 CaseSetRef）。结构类型，保持本文件零 import。
 */
export interface CaseSet {
  id: string
  canonicalization: string
  case_count: number
  /** 这版定义是否已固化在库里、可以按 id 反查。 */
  recorded: boolean
  recorded_at?: string | null
  /** 定义行缺失时无从判断，为 null。 */
  drift?: { changed: string[]; missing: string[]; unchanged: number } | null
}

/**
 * 版本标识的短形式。前缀必须留着：`cs1:` 说的就是「这是内容决定的标识」，
 * 掐掉它只剩一串十六进制，读者就无从判断它是什么。
 */
export function shortVersion(version: string): string {
  const separator = version.indexOf(':')
  if (separator < 0) return version
  const scheme = version.slice(0, separator)
  const digest = version.slice(separator + 1)
  if (digest.length <= 8) return version
  return scheme + ':' + digest.slice(0, 8) + '…'
}

/**
 * 「这批跑的是哪一版用例集」。
 *
 * 没有版本记录时如实说「无版本记录」：不写「版本：未知」那种像是缺了一个默认值的说法，
 * 也不按当前用例反推一个版本号——那等于给历史批次补一个它从来没有过的前提。
 */
export function caseSetLabel(caseSet: CaseSet | null | undefined): string {
  if (!caseSet) return '用例集版本：无版本记录（这一批建于版本化之前）'
  const short = shortVersion(caseSet.id)
  if (!caseSet.recorded) return '用例集版本 ' + short + '：定义已反查不到'
  return '用例集版本 ' + short + '（' + caseSet.case_count + ' 条用例）'
}

/**
 * 用例自本批之后被改动过的提示；没变过就没有这句话（不给「一切正常」的噪音）。
 *
 * 这句话必须同时说清**归属没有变**：只说「用例变了」，读者很容易以为这一批的结论也跟着
 * 变了。事实上这一批每一格跑的都是提交那一刻冻结的定义。
 */
export function caseSetDriftNotice(caseSet: CaseSet | null | undefined): string {
  const drift = caseSet?.drift
  if (!drift) return ''
  const parts: string[] = []
  if (drift.changed.length > 0) {
    parts.push('有 ' + drift.changed.length + ' 条用例在本批提交之后被改动')
  }
  if (drift.missing.length > 0) {
    parts.push('有 ' + drift.missing.length + ' 条用例已经找不到')
  }
  if (parts.length === 0) return ''
  return parts.join('，') + '；本批结论仍归属提交那一刻的定义。'
}

/**
 * 用例页上「最近一次结论」的定义前提。
 *
 * 只在**记录过**、且与当前定义不一致时才说话：没有记录就说「一致」，等于把「不知道」写成
 * 「没问题」——那是这一层最不该犯的错。
 */
export function definitionDriftNotice(item: {
  definition_digest?: string | null
  last_definition_digest?: string | null
} | null | undefined): string {
  const last = item?.last_definition_digest
  if (!item || !last || last === item.definition_digest) return ''
  return '最近一次结论是在另一版定义下得出的：当前定义已改动，那次结论不描述现在这份定义。'
}
