/**
 * 「无法判断」的成因分类：与 Python 侧 `InconclusiveCode` 逐字一致。
 *
 * 两份定义必须同步；对齐清单见 docs/replay-semantics.md 第 8 节。取值集合是**闭集**：
 * `reasonOf` 对集合之外的取值返回 `unknown` 并保留原文，绝不猜成某个已知成因。
 *
 * 码与说明分离：`code` 稳定可判定，`detail` 给人看。整句散文只允许出现在 `detail`。
 */
export type InconclusiveCode =
  | 'incomplete_recording'
  | 'event_sequence_gap'
  | 'redacted_replay_data'
  | 'recording_loss'
  | 'truncated_context'
  | 'missing_recorded_response'
  | 'missing_initial_state'
  | 'model_context_changed'
  | 'final_output_changed'
  | 'side_effect_blocked'
  | 'unknown';

export interface InconclusiveReason { code: InconclusiveCode; detail: string; }

/** 闭集本身。顺序也是文档与控制台映射的顺序。 */
export const CAUSE_CODES = [
  'incomplete_recording', 'event_sequence_gap', 'redacted_replay_data', 'recording_loss',
  'truncated_context', 'missing_recorded_response', 'missing_initial_state',
  'model_context_changed', 'final_output_changed', 'side_effect_blocked', 'unknown',
] as const satisfies readonly InconclusiveCode[];

const KNOWN = new Set<string>(CAUSE_CODES);

/** 历史自由文本里的码与别名。长别名在前：`recording_loss` 是 `replay_recording_loss` 的子串。 */
const LEGACY: Record<string, InconclusiveCode> = {
  replay_recording_loss: 'recording_loss', recording_loss: 'recording_loss',
  missing_recorded_response: 'missing_recorded_response', missing_model_response: 'missing_recorded_response',
  no_recording: 'missing_recorded_response', 'no recorded response': 'missing_recorded_response',
  incomplete_recording: 'incomplete_recording', 'missing boundary': 'incomplete_recording',
  unsupported_or_corrupt_bundle: 'incomplete_recording',
  'boundary or event sequence gap': 'event_sequence_gap', 'event sequence gap': 'event_sequence_gap',
  'sequence gap': 'event_sequence_gap', early_end: 'event_sequence_gap',
  redacted_replay_data: 'redacted_replay_data', redacted: 'redacted_replay_data',
  'truncated messages': 'truncated_context', truncated: 'truncated_context',
  unsupported_context: 'missing_initial_state',
  model_context_changed: 'model_context_changed', final_output_changed: 'final_output_changed',
  side_effect_gate: 'side_effect_blocked', side_effect_blocked: 'side_effect_blocked',
  side_effect_executed: 'side_effect_blocked',
};

/** 用已知码构造成因；码不在闭集内时落到 `unknown` 并保留原文。 */
export function cause(code: InconclusiveCode, detail = ''): InconclusiveReason {
  return { code, detail };
}

/** 把任意来源（结构、字符串、历史包里的自由文本）解析成因。永不抛异常。 */
export function reasonOf(value: unknown): InconclusiveReason {
  if (value === null || value === undefined) return { code: 'unknown', detail: '' };
  if (typeof value === 'string') return legacyReason(value);
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if ('code' in record) {
      const raw = String(record.code ?? '');
      const detail = String(record.detail ?? '');
      return KNOWN.has(raw) ? { code: raw as InconclusiveCode, detail } : { code: 'unknown', detail: detail || raw };
    }
    for (const key of ['reason', 'text', 'message', 'detail']) {
      const text = record[key];
      if (typeof text === 'string' && text) return legacyReason(text);
    }
  }
  return { code: 'unknown', detail: String(value) };
}

/**
 * 把历史自由文本 reason 落成结构化成因。能认出码的认码，认不出的落 `unknown`
 * 并**原样保留**原文，绝不猜成某个已知成因。
 */
export function legacyReason(text: unknown): InconclusiveReason {
  const raw = (text === null || text === undefined ? '' : String(text)).trim();
  if (!raw) return { code: 'unknown', detail: '' };
  if (KNOWN.has(raw)) return { code: raw as InconclusiveCode, detail: '' };

  const split = raw.indexOf(':');
  const head = (split === -1 ? raw : raw.slice(0, split)).trim();
  const tail = split === -1 ? '' : raw.slice(split + 1).trim();
  if (KNOWN.has(head)) return { code: head as InconclusiveCode, detail: tail };
  if (LEGACY[head]) {
    // 前缀有歧义时以后半句为准（见 Python 侧同一处注释）。
    const specific = tail ? codeInText(tail) : undefined;
    if (specific && specific !== LEGACY[head]) return { code: specific, detail: tail };
    return { code: LEGACY[head], detail: tail };
  }

  // 整句散文里认码：别名按长度降序，长别名优先。
  const known = codeInText(raw);
  if (known) return { code: known, detail: '' };
  return { code: 'unknown', detail: raw };
}

/** 整段文本里认码。别名按长度降序，长别名优先（短名常是长名的子串）。 */
function codeInText(text: string): InconclusiveCode | undefined {
  for (const alias of Object.keys(LEGACY).sort((a, b) => b.length - a.length)) {
    if (text.includes(alias) || text.includes(alias.replace(/_/g, ' '))) return LEGACY[alias];
  }
  return undefined;
}

/** 从同时成立的多个成因里取最根本的一个。 */
export const CAUSE_PRECEDENCE: Record<InconclusiveCode, number> = {
  incomplete_recording: 0, event_sequence_gap: 1, redacted_replay_data: 2, recording_loss: 3,
  truncated_context: 4, missing_initial_state: 5, missing_recorded_response: 6,
  model_context_changed: 7, final_output_changed: 8, side_effect_blocked: 9, unknown: 10,
};

export function mostSignificant(causes: (InconclusiveReason | undefined | null)[]): InconclusiveReason | undefined {
  return causes.filter((item): item is InconclusiveReason => !!item)
    .sort((a, b) => CAUSE_PRECEDENCE[a.code] - CAUSE_PRECEDENCE[b.code])[0];
}
