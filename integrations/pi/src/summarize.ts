/**
 * 从**原始回放包**重建一轮真实模型运行的统计：离线、确定性、不调用任何模型。
 *
 * 报告里引用的每一个数字都应当能用这条命令重新算出来：
 *
 *     npm run summarize -- --dir artifacts/round8 --out artifacts/round8/summary.json
 *
 * 它同时给出两个口径，因为它们不是一回事：
 *   * 严格判定 verdict：答案与**全部**引用逐字符合任务集里的预期（平台用例用的就是这个）；
 *   * 内容分类 kind：把「答对了但引用不全」（bad_evidence）、「答案与引用都对，只是 JSON 外面
 *     多写了说明」（format_only）、「答错」（wrong_answer）分开。
 */
import { parseArgs } from 'node:util';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { evaluate, type Assertion } from './cases.ts';
import { classify } from './audit.ts';
import type { Bundle } from './core.ts';

const assets = fileURLToPath(new URL('../validation/', import.meta.url));
const { values } = parseArgs({ options: Object.fromEntries(['dir','suite','out'].map(k=>[k,{type:'string' as const}])) });
const required = (k: string) => { if (!values[k]) throw new Error(`--${k} is required`); return String(values[k]); };
const round6 = (n: number) => Math.round(n * 1e6) / 1e6;

function tokensOf(bundle: Bundle) {
  return bundle.steps.reduce((n, step) => n + (step.kind === 'model_call' && typeof step.output?.usage?.totalTokens === 'number' ? step.output.usage.totalTokens : 0), 0);
}
function costOf(bundle: Bundle): number | null {
  let total = 0;
  for (const step of bundle.steps) {
    if (step.kind !== 'model_call') continue;
    const cost = step.output?.usage?.cost?.total;
    if (typeof cost !== 'number' || !Number.isFinite(cost)) return null;
    total += cost;
  }
  return round6(total);
}

async function main() {
  const dir = path.resolve(required('dir'));
  const suite = JSON.parse(await readFile(values.suite ? path.resolve(String(values.suite)) : path.join(assets, 'suite.json'), 'utf8'));
  const expected = new Map<string, any>(suite.tasks.map((t: any) => [t.id, t]));
  const files = await readdir(dir);
  const samples: any[] = [];
  const records: any[] = [];
  for (const file of files.sort()) {
    const sample = /^(.*)\.(baseline|grounded)\.(\d+)\.json$/.exec(file);
    const record = /^(.*)\.record\.json$/.exec(file);
    if (!sample && !record) continue;
    const bundle: Bundle = JSON.parse(await readFile(path.join(dir, file), 'utf8'));
    const task = (sample ?? record)![1];
    const spec = expected.get(task);
    if (!spec) throw new Error(`回放包 ${file} 的任务 ${task} 不在任务集里：用 --suite 指定与这一轮相同的任务集。`);
    const base = { task, file, run: bundle.id, parentRunId: bundle.parent ?? null, modelCalls: bundle.steps.filter(s=>s.kind==='model_call').length,
      toolCalls: bundle.steps.filter(s=>s.kind==='tool_call').length, tokens: tokensOf(bundle), costUsd: costOf(bundle),
      startedAt: bundle.started, endedAt: bundle.ended, complete: bundle.complete, status: bundle.status,
      answer: bundle.final.length <= 4000 ? bundle.final : bundle.final.slice(0, 4000) + '…[截断]' };
    if (record) { records.push(base); continue; }
    const arm = sample![2];
    const assertions: Assertion[] = [{type:'no_error'},{type:'json_claim',value:{id:task,answer:spec.answer,evidence:spec.evidence}}];
    samples.push({ ...base, arm, sample: Number(sample![3]), verdict: evaluate(bundle, assertions).verdict,
      assertions: evaluate(bundle, assertions).results, kind: classify(bundle, spec).kind,
      claimAnswer: classify(bundle, spec).answer, cause: bundle.cause ?? null });
  }
  const arms = ['baseline','grounded'];
  const perTask = [...expected.keys()].map(id => {
    const row: any = { task: id, answer: expected.get(id).answer };
    for (const arm of arms) {
      const list = samples.filter(s => s.task === id && s.arm === arm);
      const count = (v: string) => list.filter(s => s.verdict === v).length;
      const kinds: Record<string, number> = {};
      for (const s of list) kinds[s.kind] = (kinds[s.kind] ?? 0) + 1;
      row[arm] = { samples: list.length, passed: count('passed'), failed: count('failed'), inconclusive: count('inconclusive'),
        error: count('error'), determinable: count('passed') + count('failed'), kinds,
        modelCalls: list.reduce((n,s)=>n+s.modelCalls,0), tokens: list.reduce((n,s)=>n+s.tokens,0),
        costUsd: round6(list.reduce((n,s)=>n+(s.costUsd ?? 0),0)), costUnknown: list.some(s=>s.costUsd===null) };
    }
    return row;
  });
  const totals = { samples: samples.length, records: records.length,
    sampleModelCalls: samples.reduce((n,s)=>n+s.modelCalls,0), recordModelCalls: records.reduce((n,s)=>n+s.modelCalls,0),
    tokens: samples.reduce((n,s)=>n+s.tokens,0) + records.reduce((n,s)=>n+s.tokens,0),
    costUsd: round6(samples.reduce((n,s)=>n+(s.costUsd ?? 0),0) + records.reduce((n,s)=>n+(s.costUsd ?? 0),0)),
    costUnknown: samples.some(s=>s.costUsd===null) || records.some(r=>r.costUsd===null),
    verdicts: Object.fromEntries(arms.map(arm => [arm, {
      passed: samples.filter(s=>s.arm===arm&&s.verdict==='passed').length,
      failed: samples.filter(s=>s.arm===arm&&s.verdict==='failed').length,
      inconclusive: samples.filter(s=>s.arm===arm&&s.verdict==='inconclusive').length,
      error: samples.filter(s=>s.arm===arm&&s.verdict==='error').length }])),
    kinds: Object.fromEntries(arms.map(arm => [arm, samples.filter(s=>s.arm===arm).reduce((acc,s)=>{acc[s.kind]=(acc[s.kind]??0)+1;return acc;},{})])) };
  const summary = { dir: path.relative(process.cwd(), dir), suite: path.relative(process.cwd(), values.suite?path.resolve(String(values.suite)):path.join(assets,'suite.json')), commit: suite.commit, perTask, totals, samples, records };
  if (values.out) await writeFile(path.resolve(String(values.out)), JSON.stringify(summary, null, 2));
  console.log(`# ${summary.dir}（commit ${summary.commit}）`);
  console.log('');
  console.log('| 任务 | 条件 | 样本 | 严格通过 | 严格未通过 | 可判断 | 内容分类 | 模型调用 | token | 成本(估算) |');
  console.log('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |');
  for (const row of perTask) for (const arm of arms) {
    const cell = row[arm];
    console.log(`| ${row.task} | ${arm} | ${cell.samples} | ${cell.passed} | ${cell.failed} | ${cell.determinable}/${cell.samples} | ${JSON.stringify(cell.kinds)} | ${cell.modelCalls} | ${cell.tokens} | ${cell.costUsd} |`);
  }
  console.log('');
  console.log(JSON.stringify(totals, null, 2));
  if (values.out) console.log(`写出：${path.resolve(String(values.out))}`);
}
main().catch(e => { console.error(e instanceof Error ? (e.stack ?? e.message) : String(e)); process.exitCode = 3; });
