import { parseArgs } from 'node:util';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { fresh, save, upload, type Bundle } from './core.ts';
import { run } from './runner.ts';
import { snapshot } from './workspace.ts';
import { evaluate, type Assertion } from './cases.ts';

const {values}=parseArgs({options:Object.fromEntries(['repo','provider','model','out','auth-path','models-path','endpoint','only','samples','tool-source'].map(k=>[k,{type:'string' as const}]))});
const required=(k:string)=>{if(!values[k])throw new Error(`--${k} is required`);return String(values[k]);};
const assets=fileURLToPath(new URL('../validation/',import.meta.url));
async function main() {
  const repo=path.resolve(required('repo')),provider=required('provider'),model=required('model'),out=path.resolve(required('out'));
  await mkdir(out,{recursive:true});
  const suite=JSON.parse(await readFile(path.join(assets,'suite.json'),'utf8'));
  const only=new Set(String(values.only??'').split(',').map(s=>s.trim()).filter(Boolean));
  const samples=Math.max(1,Number(values.samples??3));
  const toolSource=String(values['tool-source']??'snapshot');
  if(!['recorded','snapshot'].includes(toolSource))throw new Error('Invalid --tool-source');
  const tasks=suite.tasks.filter((t:any)=>!only.size||only.has(t.id));
  if(!tasks.length)throw new Error('No task matched --only');
  const prompts={baseline:await readFile(path.join(assets,'baseline.txt'),'utf8'),grounded:await readFile(path.join(assets,'grounded.txt'),'utf8')};
  const rows:any[]=[];let negative:any;let stopReason:string|undefined;
  const checkout=await snapshot(repo,suite.commit);
  try {
    // Verify every manually selected expected citation against the pinned source before paying for a model.
    for(const task of tasks) for(const evidence of task.evidence) {
      const lines=(await readFile(path.join(checkout.root,evidence.path),'utf8')).split(/\r?\n/);
      if(lines[evidence.line-1]?.trim()!==evidence.quote.trim())throw new Error(`Stale expected evidence: ${task.id}`);
    }
    for(const task of tasks) {
      const question=`${task.question}\n需要检查的文件：${task.files.join('、')}\n最终仅输出 JSON：{"claims":[{"id":"${task.id}","answer":布尔值或字符串,"evidence":[{"path":"相对文件路径","line":准确行号,"quote":"该行代码原文（可去除首尾空白）"}]}]}。不得引用文档代替实现。`;
      const assertions:Assertion[]=[{type:'no_error'},{type:'json_claim',value:{id:task.id,answer:task.answer,evidence:task.evidence}}];
      await writeFile(path.join(out,`${task.id}.task.txt`),question);
      await writeFile(path.join(out,`${task.id}.assertions.json`),JSON.stringify(assertions,null,2));
      const parent=fresh(question,prompts.baseline,provider,model,suite.commit);
      await run({bundle:parent,root:checkout.root,authPath:values['auth-path'] as string,modelsPath:values['models-path'] as string});
      const stored=await save(path.join(out,`${task.id}.record.json`),parent);
      await upload(stored,String(values.endpoint??'http://127.0.0.1:7710'));
      rows.push(row(task.id,'record',0,stored,evaluate(stored,assertions).verdict));
      if(stored.status!=='succeeded'||!stored.complete) {stopReason=`Recording ${task.id}: ${stored.reason}`;break;}
      if(!negative) {
        const control={...stored,final:JSON.stringify({claims:[{id:task.id,answer:!task.answer,evidence:task.evidence}]})};
        negative={label:'synthetic negative control; excluded from model statistics',...evaluate(control,assertions)};
        if(negative.verdict!=='failed')throw new Error('Negative control unexpectedly passed');
      }
      for(const arm of ['baseline','grounded'] as const) for(let sample=1;sample<=samples;sample++) {
        const child=fresh(question,prompts[arm],provider,model,suite.commit);child.mode='regress';child.parent=stored.id;
        await run({bundle:child,parent:stored,toolSource:toolSource as 'recorded'|'snapshot',
          root:toolSource==='snapshot'?checkout.root:undefined,
          authPath:values['auth-path'] as string,modelsPath:values['models-path'] as string});
        const evaluation=evaluate(child,assertions);child.verdict=evaluation.verdict;
        const saved=await save(path.join(out,`${task.id}.${arm}.${sample}.json`),child);
        const transport=await upload(saved,String(values.endpoint??'http://127.0.0.1:7710'));
        rows.push({...row(task.id,arm,sample,saved,saved.verdict!),results:evaluation.results,uploaded:transport.uploaded});
        console.log(JSON.stringify(rows.at(-1)));
      }
    }
  } finally {
    try {await checkout.close();} catch(e) {stopReason=`Checkout retained: ${String(e)}`;}
    const result={commit:suite.commit,provider,model,toolSource,negativeControl:negative,stopReason,rows};
    await writeFile(path.join(out,'results.json'),JSON.stringify(result,null,2));
    const table=rows.map(r=>`| ${r.task} | ${r.arm} | ${r.sample} | ${r.verdict} | ${r.modelCalls} | ${r.ms} | ${r.reason??''} |`).join('\n');
    const summaries=['baseline','grounded'].map(arm=>{
      const runs=rows.filter(r=>r.arm===arm),passed=runs.filter(r=>r.verdict==='passed').length,covered=runs.filter(r=>['passed','failed'].includes(r.verdict)).length;
      return `${arm}: ${passed}/${runs.length} 通过，${covered}/${runs.length} 可判断。未执行样本不进入分母。`;
    });
    await writeFile(path.join(out,'report.md'),`# pi 代码调查回归验证\n\n固定提交：${suite.commit}。模型：${provider}/${model}。回归工具来源：${toolSource}。\n\n${summaries.join('\n\n')}\n\n${stopReason??'所有计划样本已执行。'}\n\n| 任务 | Prompt | 样本 | 结论 | 真实模型调用 | 耗时 ms | 原因 |\n| --- | --- | --- | --- | --- | --- | --- |\n${table}\n\n评测要求结构化答案和准确源码引用；失败也可能是格式或引用不匹配，需要结合回放包复核。录制不足（inconclusive）不能解释为模型退化，也不能算断言失败。没有观察到失败到通过的变化时，不声称修复效果。负向控制为人工构造，不计入真实样本。\n`);
    console.log(JSON.stringify({report:path.join(out,'report.md'),stopReason}));
  }
}
function row(task:string,arm:string,sample:number,b:Bundle,verdict:string) {
  return {task,arm,sample,verdict,run:b.id,reason:b.reason,modelCalls:b.steps.filter(s=>s.kind==='model_call').length,
    toolCalls:b.steps.filter(s=>s.kind==='tool_call').length,tokens:b.steps.reduce((n,s)=>n+(s.kind==='model_call'?(s.output?.usage?.totalTokens??0):0),0),ms:Date.parse(b.ended)-Date.parse(b.started)};
}
main().catch(e=>{console.error(String(e));process.exitCode=3;});
