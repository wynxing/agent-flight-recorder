/**
 * 成因解析的 pi 侧契约：认得出码的认码，认不出的落 `unknown` 并保留原文。
 *
 * 与 Python 侧 `InconclusiveReason.legacy` 断言的是同一批行为（跨语言一致性
 * 由 server/tests/test_reason_contract.py 的对照用例再钉一层）。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { legacyReason, reasonOf } from '../src/reasons.ts';

test('a code that says the side effect ran is never relabelled as blocked', () => {
  // `side_effect_executed` 说的是「真的执行了副作用」，与「被拦截」语义恰好相反：
  // 认不出就落 `unknown` 并保留原文，绝不猜成 blocked。
  const parsed = legacyReason('side_effect_executed');
  assert.notEqual(parsed.code, 'side_effect_blocked');
  assert.equal(parsed.code, 'unknown');
  assert.equal(parsed.detail, 'side_effect_executed');
  assert.equal(legacyReason('side_effect_executed: step 4').code, 'unknown');
  // 真正的拦截信号不受影响：「被拦截」与「执行了副作用」本来就是两件事。
  assert.equal(legacyReason('side_effect_blocked').code, 'side_effect_blocked');
  assert.equal(legacyReason('side_effect_gate').code, 'side_effect_blocked');
});

test('underscore and space spellings are both recognised', () => {
  assert.equal(legacyReason('no_recording').code, 'missing_recorded_response');
  assert.equal(legacyReason('no recording').code, 'missing_recorded_response');
  assert.equal(legacyReason('truncated messages').code, 'truncated_context');
  assert.equal(legacyReason('the run hit early_end').code, 'event_sequence_gap');
  assert.equal(legacyReason('the run hit early end').code, 'event_sequence_gap');
});

test('values outside the closed set fall to unknown and keep the original text', () => {
  assert.deepEqual(reasonOf({ code: 'not_a_code', detail: 'x' }), { code: 'unknown', detail: 'x' });
  assert.deepEqual(legacyReason('something nobody classified'), {
    code: 'unknown',
    detail: 'something nobody classified',
  });
});

