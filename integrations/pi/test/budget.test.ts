import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { fresh } from '../src/core.ts';
import { run, assertProviderCredentials } from '../src/runner.ts';
import {
  BudgetLedger, NOT_STARTED, beginCell, budgetSpecFromOptions, estimateBatch, usageDetail,
} from '../src/budget.ts';

function reply(options: {text?: string; tool?: {name: string; args: any}; cost?: number | null; stopReason?: string} = {}) {
  const {text, tool, cost = 0, stopReason = tool ? 'toolUse' : 'stop'} = options;
  const content: any[] = tool ? [{type: 'toolCall', id: 'call-1', name: tool.name, arguments: tool.args}] : [{type: 'text', text: text ?? 'done'}];
  const total = cost == null ? null : cost;
  const usage = total == null
    ? undefined
    : {input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total}};
  const message: any = {role: 'assistant', content, api: 'openai-completions', provider: 'test', model: 'test', usage, stopReason, timestamp: 123};
  const stream = createAssistantMessageEventStream();
  queueMicrotask(() => {
    if (stopReason === 'error') { stream.push({type: 'error', reason: 'error', error: message}); stream.end(message); }
    else { stream.push({type: 'done', reason: stopReason as any, message}); stream.end(message); }
  });
  return stream;
}

test('a ledger books only completed calls, and reaching an allowance counts as the ceiling', () => {
  const calls = new BudgetLedger({max_model_calls: 2});
  assert.equal(calls.exceededBy(), null);
  calls.noteModelCall(0.01);
  assert.equal(calls.exceededBy(), null);
  calls.noteModelCall(0.01);
  // 「达到」就是触顶：>= 而不是 >，与 Python 侧同一处判定。
  assert.equal(calls.exceededBy(), 'model_calls');
  assert.deepEqual(calls.remaining(), {max_cost_usd: null, max_model_calls: 0});
});

test('cost checked only when it is known, and the calls dimension is decided first', () => {
  const both = new BudgetLedger({max_model_calls: 1, max_cost_usd: 0.5});
  both.noteModelCall(0.9);
  // 两个维度都到了：先判调用次数（与 Python 侧同一顺序）。
  assert.equal(both.exceededBy(), 'model_calls');

  const unpriced = new BudgetLedger({max_cost_usd: 0.000001});
  unpriced.noteModelCall(null);
  // 成本未知不是 0：这一维因此不参与判定，而不是把未知当成 0 立刻停下。
  assert.equal(unpriced.costUsedUsd, null);
  assert.equal(unpriced.exceededBy(), null);
  assert.deepEqual(unpriced.remaining(), {max_cost_usd: null, max_model_calls: null});
  const usage = unpriced.usage();
  assert.equal(usage.cost_unknown, true);
  assert.match(usageDetail(usage), /成本未知/);

  const priced = new BudgetLedger({max_cost_usd: 0.5});
  priced.noteModelCall(0.5);
  assert.equal(priced.exceededBy(), 'cost');
  assert.match(usageDetail(priced.usage()), /没有因预算停止/);
});

test('a known part-sum must not stand in for an unknown total when deciding the cost ceiling', () => {
  // 已知部分已经有数（0.01），总量却是**未知**（后面有一次调用无法定价）。
  // 拿已知部分冒充总额参与判定，就是把「说不清花了多少」说成「已经花到上限」：上限 0 这种
  // 极端配置下尤其明显。未知 ⇒ 这一维不参与判定，与 Python 侧 cost.py 同一条立场。
  const ledger = new BudgetLedger({max_cost_usd: 0});
  ledger.noteModelCall(0.01);
  ledger.noteModelCall(null);
  assert.equal(ledger.costUsedUsd, null);
  assert.equal(ledger.exceededBy(), null);
});

test('a batch hands each cell what is left and stops starting new cells at the ceiling', () => {
  const ledger = new BudgetLedger({max_model_calls: 3});
  assert.deepEqual(beginCell(ledger), {max_cost_usd: null, max_model_calls: 3});
  ledger.noteModelCall(0.001);
  assert.deepEqual(beginCell(ledger), {max_cost_usd: null, max_model_calls: 2});
  ledger.noteModelCall(0.001);
  ledger.noteModelCall(0.001);
  // 到点之后不再启动新格子：这一维要**先**写进账，格子才有终态可言。
  assert.equal(beginCell(ledger), null);
  assert.equal(ledger.stoppedBy, 'model_calls');
  assert.equal(ledger.usage().exceeded, true);
  assert.equal(NOT_STARTED, 'not_started');
});

test('the estimate never turns a missing price into a number', () => {
  const known = estimateBatch([{task: 'a', calls: [{tokens: 100, costUsd: 0.002}, {tokens: 100, costUsd: 0.002}]}], 6);
  assert.equal(known.cells, 6);
  assert.equal(known.model_calls, 12);
  assert.equal(known.cost_usd, 0.024);
  assert.match(known.detail, /不包含/);

  const partial = estimateBatch([
    {task: 'a', calls: [{tokens: 100, costUsd: 0.002}]},
    {task: 'b', calls: [{tokens: 100, costUsd: null}]},
  ], 3);
  assert.equal(partial.cost_usd, null);
  assert.equal(partial.model_calls, 6);
  assert.match(partial.detail, /无法预估/);
});

test('a declared budget is optional and validated', () => {
  assert.equal(budgetSpecFromOptions({}), null);
  assert.deepEqual(budgetSpecFromOptions({maxCostUsd: '1.5', maxModels: '4'}), {max_cost_usd: 1.5, max_model_calls: 4});
  assert.throws(() => budgetSpecFromOptions({maxModels: '0.5'}), /Invalid --budget-models/);
  assert.throws(() => budgetSpecFromOptions({maxCostUsd: '-1'}), /Invalid --budget-cost-usd/);
});

test('a declared budget stops the real SDK loop and ends inconclusive, never as a verdict', async () => {
  const parent = fresh('t', 'p', 'test', 'test', 'c');
  parent.steps = [{kind: 'tool_call', name: 'read', input: {path: 'x'}, output: {content: [{type: 'text', text: 'x'}]}, duration_ms: 0}];
  const b = fresh('t', 'p', 'test', 'test', 'c');
  b.mode = 'regress';
  let calls = 0;
  await run({
    bundle: b, parent, budget: {max_model_calls: 1},
    stream: () => (++calls === 1 ? reply({tool: {name: 'read', args: {path: 'x'}}, cost: 0.01}) : reply({text: 'should never run'})),
  });
  assert.equal(b.status, 'aborted');
  assert.equal(b.reason?.includes('已在步边界停止'), true);
  assert.equal(b.complete, false);
  assert.equal(b.verdict, 'inconclusive');
  assert.equal(b.cause?.code, 'budget_exceeded');
  assert.equal(b.steps.filter(s => s.kind === 'model_call').length, 1);
  assert.equal(b.budget?.exceeded, true);
  assert.equal(b.budget?.stopped_by, 'model_calls');
  assert.equal(b.budget?.model_calls_used, 1);
  assert.equal(b.budget?.cost_used_usd, 0.01);
});

test('the cost dimension stops the loop too, and an unpriced call makes cost unknown', async () => {
  const parent = fresh('t', 'p', 'test', 'test', 'c');
  parent.steps = [{kind: 'tool_call', name: 'read', input: {path: 'x'}, output: {}, duration_ms: 0}];
  const b = fresh('t', 'p', 'test', 'test', 'c');
  b.mode = 'regress';
  let calls = 0;
  await run({
    bundle: b, parent, budget: {max_cost_usd: 0.4},
    stream: () => (++calls === 1 ? reply({tool: {name: 'read', args: {path: 'x'}}, cost: 0.5}) : reply({text: 'should never run'})),
  });
  assert.equal(b.cause?.code, 'budget_exceeded');
  assert.equal(b.budget?.stopped_by, 'cost');
  assert.equal(b.budget?.cost_used_usd, 0.5);

  const unpriced = fresh('t', 'p', 'test', 'test', 'c');
  unpriced.mode = 'regress';
  await run({bundle: unpriced, parent, budget: {max_cost_usd: 0.001}, stream: () => reply({text: 'x', cost: null})});
  // 拿不到用量的那次调用按「未知」记账：成本维因此不参与判定，也不会被说成没花钱。
  assert.equal(unpriced.budget?.cost_unknown, true);
  assert.equal(unpriced.budget?.cost_used_usd, null);
  assert.equal(unpriced.budget?.exceeded, false);
});

test('without a declared budget nothing changes: no accounting object, the old kill switch stays', async () => {
  const b = fresh('t', 'p', 'test', 'test', 'c');
  b.mode = 'regress';
  await run({bundle: b, stream: () => reply({text: 'x', cost: 0.01})});
  assert.equal(b.budget, undefined);

  const capped = fresh('t', 'p', 'test', 'test', 'c');
  capped.mode = 'regress';
  const tape = fresh('t', 'p', 'test', 'test', 'c');
  tape.steps = [{kind: 'tool_call', name: 'read', input: {path: 'x'}, output: {}, duration_ms: 0}];
  let cappedCalls = 0;
  await run({bundle: capped, parent: tape, maxModels: 1,
    stream: () => (++cappedCalls === 1 ? reply({tool: {name: 'read', args: {path: 'x'}}}) : reply({text: 'should never run'}))});
  assert.equal(capped.status, 'aborted');
  assert.equal(capped.reason, 'model_budget_exceeded');
  assert.equal(capped.budget, undefined);
});

test('missing credentials stop before any real call, with the variable named', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'msee-cred-'));
  try {
    const file = path.join(root, 'models.json');
    await writeFile(file, JSON.stringify({providers: {gw: {baseUrl: 'https://example.invalid/v1', api: 'openai-completions', apiKey: '$MSEE_TEST_ABSENT_KEY', models: [{id: 'm'}]}}}));
    await assert.rejects(assertProviderCredentials(file), /MSEE_TEST_ABSENT_KEY/);
    process.env.MSEE_TEST_PRESENT_KEY = 'set-for-test';
    try {
      await writeFile(file, JSON.stringify({providers: {gw: {baseUrl: 'https://example.invalid/v1', api: 'openai-completions', apiKey: '$MSEE_TEST_PRESENT_KEY', models: [{id: 'm'}]}}}));
      await assertProviderCredentials(file);
    } finally {
      delete process.env.MSEE_TEST_PRESENT_KEY;
    }
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});


/**
 * 这一条是第 8 轮构建会话真机上抓到的缺陷的反证：批量账本当时**没有把每一格的用量记回来**，
 * 于是 declared 的 400 次上限一直是「已用 0」，额度永远不会缩小、也永远不会触顶——一个看起来
 * 声明了上限、实际一次都没生效的账本。这里的调用顺序与 validate.ts 的批量循环一致。
 */
test('a batch ledger drains as cells finish, so the declared cap really binds', () => {
  const ledger = new BudgetLedger({max_model_calls: 5, max_cost_usd: 1});
  const seen: number[] = [];
  let stopped = false;
  for (let cell = 0; cell < 10; cell++) {
    const budget = beginCell(ledger);
    if (budget === null) { stopped = true; break; }
    seen.push(budget.max_model_calls ?? -1);
    // 每一格真的跑两次调用（成本可定价）。
    ledger.mergeCell({model_calls_used: 2, cost_used_usd: 0.01});
  }
  assert.deepEqual(seen, [5, 3, 1]);
  assert.equal(stopped, true);
  assert.equal(ledger.stoppedBy, 'model_calls');
  const usage = ledger.usage();
  assert.equal(usage.model_calls_used, 6);
  assert.equal(usage.exceeded, true);
});

test('booked-back usage keeps unknown cost unknown, never zero', () => {
  const ledger = new BudgetLedger({max_cost_usd: 1});
  ledger.mergeCell({model_calls_used: 2, cost_used_usd: 0.02});
  ledger.mergeCell({model_calls_used: 1, cost_used_usd: null});
  assert.equal(ledger.usage().model_calls_used, 3);
  // 有一格算不出成本，整批的金额就是未知：未知不会被当成 0，成本这一维也就不再参与判定。
  assert.equal(ledger.costUsedUsd, null);
  assert.equal(ledger.exceededBy(), null);
  assert.equal(ledger.usage().cost_unknown, true);
  assert.deepEqual(ledger.remaining(), {max_cost_usd: null, max_model_calls: null});
});


