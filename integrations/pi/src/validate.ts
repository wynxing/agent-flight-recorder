/**
 * 真实模型回归验证。四个阶段分开，是为了「先声明上限再花钱」这件事能被执行：
 *
 * * `check`    —— 免费：核对每个任务的预期引用在固定提交上仍然成立（过期引用立即拒绝）。
 * * `record`   —— 花钱：每个任务录制一次，作为两个条件的公共父 Run。
 * * `estimate` —— 免费：按父录制里真实发生的调用量与成本，预估整批要花多少。
 * * `regress`  —— 花钱：条件矩阵（baseline / grounded × 采样数）。
 * * `all`      —— 默认：record + regress（与加这些选项之前的路径一致）。
 *
 * 声明的上限（`--budget-models` / `--budget-cost-usd`）与平台是同一套语义：步边界硬停、
 * `stopped_by ∈ {model_calls, cost}`、没跑到的格子标成 `not_started` 且没有结论也没有成因。
 * 见 src/budget.ts。
 */
import { parseArgs } from 'node:util';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { fresh, save, upload, hash, type Bundle } from './core.ts';
import { run, assertProviderCredentials, resolveModelInfo } from './runner.ts';
import { snapshot } from './workspace.ts';
import { evaluate, type Assertion } from './cases.ts';
import {
  BudgetLedger, batchUsageDetail, beginCell, budgetSpecFromOptions, estimateBatch,
  notStartedReasonText,
  type BudgetSpec, type BudgetUsage, type RecordedCall,
} from './budget.ts';

const names = ['repo','provider','model','out','auth-path','models-path','endpoint','only','samples','tool-source','suite','phase','budget-models','budget-cost-usd'];
const {values}=parseArgs({options:Object.fromEntries(names.map(k=>[k,{type:'string' as const}]))});
const required=(k:string)=>{if(!values[k])throw new Error(`--${k} is required`);return String(values[k]);};
const assets=fileURLToPath(new URL('../validation/',import.meta.url));
const PHASES=['check','record','estimate','regress','all'];
const ARMS=['baseline','grounded'] as const;

const sha256=(text:string)=>createHash('sha256').update(text,'utf8').digest('hex');
const readJson=async(file:string)=>JSON.parse(await readFile(file,'utf8'));
async function readMaybe(file:string) { try { return await readJson(file); } catch { return undefined; } }

/** 一份包里真实发生的模型调用留下的用量（成本未知时是 null，不是 0）。 */
function liveCalls(bundle:Bundle):RecordedCall[] {
  return bundle.steps.filter(step=>step.kind==='model_call').map(step=>({
    tokens: typeof step.output?.usage?.totalTokens==='number' ? step.output.usage.totalTokens : null,
    costUsd: typeof step.output?.usage?.cost?.total==='number' ? step.output.usage.cost.total : null,
  }));
}
function tokensOf(bundle:Bundle) {
  const sum=(pick:(usage:any)=>number|undefined)=>bundle.steps.reduce((n,step)=>{
    const usage=step.kind==='model_call'?step.output?.usage:undefined;const value=usage?pick(usage):undefined;
    return n+(typeof value==='number'?value:0);
  },0);
  return {input:sum(u=>u.input),output:sum(u=>u.output),total:sum(u=>u.totalTokens)};
}
/** 一份包的成本合计。只要有一次调用无法定价，整份包的金额就是「未知」，不是 0。 */
function costOfBundle(bundle:Bundle):number|null {
  let total=0;
  for(const step of bundle.steps) {
    if(step.kind!=='model_call') continue;
    const cost=step.output?.usage?.cost?.total;
    if(typeof cost!=='number'||!Number.isFinite(cost)) return null;
    total+=cost;
  }
  return Math.round(total*1e6)/1e6;
}
function digest(bundle:Bundle) {
  return {run:bundle.id,parentRunId:bundle.parent??null,mode:bundle.mode,toolSource:bundle.toolSource??null,
    status:bundle.status,complete:bundle.complete,verdict:bundle.verdict??null,cause:bundle.cause??null,
    modelCalls:bundle.steps.filter(s=>s.kind==='model_call').length,
    toolCalls:bundle.steps.filter(s=>s.kind==='tool_call').length,
    tokens:tokensOf(bundle),costUsd:costOfBundle(bundle),
    startedAt:bundle.started,endedAt:bundle.ended,
    durationMs:Date.parse(bundle.ended)-Date.parse(bundle.started),
    budget:bundle.budget??null};
}

async function main() {
  const repo=path.resolve(required('repo')),out=path.resolve(required('out'));
  const phase=String(values.phase??'all');
  if(!PHASES.includes(phase)) throw new Error(`Invalid --phase (${PHASES.join('|')})`);
  const suitePath=path.resolve(String(values.suite??path.join(assets,'suite.json')));
  const suiteText=await readFile(suitePath,'utf8');
  const suite=JSON.parse(suiteText);
  await mkdir(out,{recursive:true});
  const declared=budgetSpecFromOptions({maxCostUsd:values['budget-cost-usd'],maxModels:values['budget-models']});
  const ledger=new BudgetLedger(declared);
  const only=new Set(String(values.only??'').split(',').map(s=>s.trim()).filter(Boolean));
  const samples=Math.max(1,Number(values.samples??3));
  const toolSource=String(values['tool-source']??'snapshot');
  if(!['recorded','snapshot'].includes(toolSource))throw new Error('Invalid --tool-source');
  const tasks=suite.tasks.filter((t:any)=>!only.size||only.has(t.id));
  if(!tasks.length)throw new Error('No task matched --only');
  const prompts={baseline:await readFile(path.join(assets,'baseline.txt'),'utf8'),grounded:await readFile(path.join(assets,'grounded.txt'),'utf8')};
  const provider=values.provider?String(values.provider):undefined,model=values.model?String(values.model):undefined;
  const endpoint=String(values.endpoint??'http://127.0.0.1:7710');
  const notStarted:any[]=[];
  const rows:any[]=[];
  const records=new Map<string,Bundle>();
  let negative:any;let stopReason:string|undefined;
  let gate:any;
  const questions=new Map<string,string>();
  for(const task of tasks) questions.set(task.id,`${task.question}\n需要检查的文件：${task.files.join('、')}\n最终仅输出 JSON：{"claims":[{"id":"${task.id}","answer":布尔值或字符串,"evidence":[{"path":"相对文件路径","line":准确行号,"quote":"该行代码原文（可去除首尾空白）"}]}]}。不得引用文档代替实现。`);
  const assertionsFor=(task:any):Assertion[]=>[{type:'no_error'},{type:'json_claim',value:{id:task.id,answer:task.answer,evidence:task.evidence}}];
  const checkout=await snapshot(repo,suite.commit);
  try {
    // 免费的第一步：每个任务的预期引用都要在固定提交上逐字成立。引用过期时立即停下，
    // 因为那样量出来的「模型答错了」其实是任务集自己错了。
    for(const task of tasks) for(const evidence of task.evidence) {
      const lines=(await readFile(path.join(checkout.root,evidence.path),'utf8')).split(/\r?\n/);
      if(lines[evidence.line-1]?.trim()!==evidence.quote.trim())throw new Error(`Stale expected evidence: ${task.id}`);
    }
    if(phase==='check') {
      // 免费的自检顺带把模型解析也做掉：名字打错、模型不在配置里，在花钱之前就会停下。
      const resolved = provider&&model
        ? await resolveModelInfo({authPath:values['auth-path'],modelsPath:values['models-path'],provider,model})
        : undefined;
      if(provider&&model&&!resolved) throw new Error(`Unknown explicit model ${provider}/${model}`);
      console.log(JSON.stringify({suite:path.relative(process.cwd(),suitePath),commit:suite.commit,
        tasks:tasks.map((t:any)=>({id:t.id,files:t.files,evidence:t.evidence.length,answer:t.answer})),
        model:resolved??null,note:'预期引用在固定提交上逐字成立。'},null,2));
      return;
    }
    if(phase!=='regress') {
      // ---------------- 录制：每个任务一次，作为两个条件的公共父 Run ----------------
      if(!provider||!model) throw new Error('--provider and --model are required to record');
      if(values['models-path']) await assertProviderCredentials(path.resolve(String(values['models-path'])));
      for(const task of tasks) {
        const file=path.join(out,`${task.id}.record.json`);
        const existing=await readMaybe(file);
        let stored:Bundle;
        if(existing && existing.complete && existing.status==='succeeded') {
          stored=existing;
        } else {
          const cell=declared?beginCell(ledger):undefined;
          if(declared&&cell===null) {notStarted.push({task:task.id,phase:'record'});continue;}
          const parent=fresh(questions.get(task.id)!,prompts.baseline,provider,model,suite.commit);
          await run({bundle:parent,root:checkout.root,authPath:values['auth-path'],modelsPath:values['models-path'],budget:cell??undefined});
          stored=await save(file,parent);
          ledger.mergeCell(stored.budget);
          await upload(stored,endpoint);
          await writeFile(path.join(out,`${task.id}.task.txt`),questions.get(task.id)!);
          await writeFile(path.join(out,`${task.id}.assertions.json`),JSON.stringify(assertionsFor(task),null,2));
        }
        records.set(task.id,stored);
        const recording=evaluate(stored,assertionsFor(task));
        rows.push({...digest(stored),task:task.id,arm:'record',sample:0,verdict:stored.complete?recording.verdict:'inconclusive',results:recording.results});
        if(stored.status!=='succeeded'||!stored.complete) {stopReason=`Recording ${task.id}: ${stored.reason}`;break;}
        if(!negative) {
          const control={...stored,final:JSON.stringify({claims:[{id:task.id,answer:!task.answer,evidence:task.evidence}]})};
          negative={label:'synthetic negative control; excluded from model statistics',...evaluate(control,assertionsFor(task))};
          if(negative.verdict!=='failed')throw new Error('Negative control unexpectedly passed');
          // 负向控制单独落盘：回归阶段是新的一次调用，不会重建它，但归档里必须有它。
          await writeFile(path.join(out,'negative-control.json'),JSON.stringify(negative,null,2));
        }
      }
    } else {
      for(const task of tasks) {
        const stored=await readMaybe(path.join(out,`${task.id}.record.json`));
        // 录制阶段被上限截断时，这些格子的样本从未启动：如实标成 not_started，而不是编一个结论。
        if(!stored||!stored.complete) {for(const arm of ARMS) for(let sample=1;sample<=samples;sample++) notStarted.push({task:task.id,arm,sample,reason:'record_missing'});continue;}
        records.set(task.id,stored);
      }
    }
    if(phase==='estimate') {
      const batch=estimateBatch([...records].map(([task,bundle])=>({task,calls:liveCalls(bundle)})),samples*ARMS.length);
      const estimate={...batch,declared:declared??null,samples,arms:[...ARMS],tasks:[...records.keys()]};
      await writeFile(path.join(out,'estimate.json'),JSON.stringify(estimate,null,2));
      console.log(JSON.stringify({estimate:path.join(out,'estimate.json'),...estimate},null,2));
      return;
    }
    if(phase==='all') {
      // 预告：录制已经完成，回归还没开始。这一步免费，且它真的会把关——预估超出声明的上限，
      // 后面的格子一个都不启动（它们会如实记成 not_started，而不是跑出去再说）。
      const remaining=ledger.remaining();
      const batch=estimateBatch([...records].map(([task,bundle])=>({task,calls:liveCalls(bundle)})),samples*ARMS.length);
      const overCost=declared&&batch.cost_usd!=null&&remaining.max_cost_usd!=null&&batch.cost_usd>remaining.max_cost_usd;
      const overCalls=declared&&remaining.max_model_calls!=null&&batch.model_calls>remaining.max_model_calls;
      gate={...batch,remaining_allowed:remaining,over_budget:Boolean(overCost||overCalls)};
      await writeFile(path.join(out,'estimate.json'),JSON.stringify({...gate,declared:declared??null,samples,arms:[...ARMS],tasks:[...records.keys()]},null,2));
      console.log(JSON.stringify({estimate:{model_calls:gate.model_calls,cost_usd:gate.cost_usd,over_budget:gate.over_budget,detail:gate.detail}}));
      if(gate.over_budget) {
        stopReason='预估超出声明的上限，回归阶段没有启动：'+gate.detail;
        for(const task of tasks) for(const arm of ARMS) for(let sample=1;sample<=samples;sample++)
          notStarted.push({task:task.id,arm,sample,reason:'estimate_over_budget'});
      }
    }
    // ---------------- 回归矩阵 ----------------
    if(!provider||!model) throw new Error('--provider and --model are required to regress');
    for(const task of tasks) {
      if(gate?.over_budget) break;
      const stored=records.get(task.id)!;
      const assertions=assertionsFor(task);
      for(const arm of ARMS) for(let sample=1;sample<=samples;sample++) {
        const cell=declared?beginCell(ledger):undefined;
        if(declared&&cell===null) {notStarted.push({task:task.id,arm,sample});continue;}
        const child=fresh(questions.get(task.id)!,prompts[arm],provider,model,suite.commit);
        child.mode='regress';child.parent=stored.id;
        await run({bundle:child,parent:stored,toolSource:toolSource as 'recorded'|'snapshot',
          root:toolSource==='snapshot'?checkout.root:undefined,
          authPath:values['auth-path'],modelsPath:values['models-path'],budget:cell??undefined});
        ledger.mergeCell(child.budget);
        const evaluation=evaluate(child,assertions);child.verdict=evaluation.verdict;
        const saved=await save(path.join(out,`${task.id}.${arm}.${sample}.json`),child);
        const transport=await upload(saved,endpoint);
        const row={...digest(saved),task:task.id,arm,sample,verdict:saved.verdict!,results:evaluation.results,uploaded:transport.uploaded};
        rows.push(row);
        console.log(JSON.stringify(row));
      }
    }
  } finally {
    try{await checkout.close();}catch(e){stopReason=`Checkout retained: ${String(e)}`;}
    if(!negative) negative=await readMaybe(path.join(out,'negative-control.json'));
    const usage=ledger.usage();
    const reasonText=notStartedReasonText(notStarted.map(n=>n.reason??'budget_exhausted'));
    const batch=declared?{declared,usage:{...usage,detail:batchUsageDetail(usage,notStarted.length,reasonText)},
      not_started_reasons:[...new Set(notStarted.map(n=>n.reason??'budget_exhausted'))],
      not_started:notStarted.length,not_started_cells:notStarted}:null;
    const samplesPlanned=tasks.length*ARMS.length*samples;
    const manifest={
      generated_at:new Date().toISOString(),
      suite:path.relative(process.cwd(),suitePath),suite_sha256:sha256(suiteText),
      tasks:tasks.map((t:any)=>t.id),commit:suite.commit,
      provider:provider??null,model:model??null,
      prompts:{baseline_sha256:hash(prompts.baseline),grounded_sha256:hash(prompts.grounded)},
      tool_source:toolSource,samples_per_arm:samples,phase,arms:[...ARMS],samples_planned:samplesPlanned,
      declared_budget:declared??null,
      estimate:gate??null,
      models_path:values['models-path']?path.relative(process.cwd(),String(values['models-path'])):null,
      negative_control:negative??null,
    };
    const archive={version:1,manifest,batch,
      records:[...records].map(([task,bundle])=>({task,...digest(bundle)})),
      samples:rows.filter(r=>r.arm!=='record'),not_started:notStarted};
    await writeFile(path.join(out,'results.json'),JSON.stringify({...manifest,stopReason,batch,rows},null,2));
    await writeFile(path.join(out,'archive.json'),JSON.stringify(archive,null,2));
    const table=rows.map(r=>`| ${r.task} | ${r.arm} | ${r.sample} | ${r.verdict} | ${r.modelCalls} | ${r.tokens.total} | ${r.costUsd===null?'未知':r.costUsd.toFixed(6)} | ${r.cause?.code??''} |`).join('\n');
    const summaries=ARMS.map(arm=>{
      const runs=rows.filter(r=>r.arm===arm);
      const count=(verdict:string)=>runs.filter(r=>r.verdict===verdict).length;
      const covered=count('passed')+count('failed');
      return `${arm}: ${runs.length} 个已执行样本中 ${count('passed')} 通过、${count('failed')} 未通过、${count('inconclusive')} 无法判断、${count('error')} 执行错误；可判断 ${covered}/${runs.length}。未执行样本不进入分母。`;
    });
    const batchLine=batch?`\n\n## 批次预算\n\n声明上限：${JSON.stringify(batch.declared)}。${batch.usage.detail}\n`:'';
    await writeFile(path.join(out,'report.md'),`# pi 代码调查回归验证\n\n固定提交：${suite.commit}。模型：${provider}/${model}。回归工具来源：${toolSource}。\n\n${summaries.join('\n\n')}${batchLine}\n${stopReason??'所有计划样本已执行。'}\n\n| 任务 | Prompt | 样本 | 结论 | 真实模型调用 | token | 成本（估算） | 成因码 |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n${table}\n\n评测要求结构化答案和准确源码引用；失败也可能是格式或引用不匹配，需要结合回放包复核。录制不足（inconclusive）不能解释为模型退化，也不能算断言失败。没有观察到失败到通过的变化时，不声称修复效果。负向控制为人工构造，不计入真实样本。成本按本地价目表（models.json）计算，是估算而不是账单。\n`);
    console.log(JSON.stringify({report:path.join(out,'report.md'),archive:path.join(out,'archive.json'),stopReason,
      batch:batch?{declared:batch.declared,stopped_by:batch.usage.stopped_by,exceeded:batch.usage.exceeded,not_started:batch.not_started}:null}));
  }
}
main().catch(e=>{console.error(String(e));process.exitCode=3;});
