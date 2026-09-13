import { parseArgs } from 'node:util';
import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import type { Bundle } from './core.ts';

/** First balanced JSON object in a text; models often wrap the answer in prose. */
export function extractJson(text: string): any | undefined {
  for (let start = text.indexOf('{'); start !== -1; start = text.indexOf('{', start + 1)) {
    let depth = 0, inString = false, escaped = false;
    for (let i = start; i < text.length; i++) {
      const ch = text[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === '\\') escaped = true;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) {
          try { return JSON.parse(text.slice(start, i + 1)); } catch { break; }
        }
      }
    }
  }
  return undefined;
}

export type FailureKind = 'pass' | 'wrong_answer' | 'bad_evidence' | 'format_only' | 'no_json';

export function classify(bundle: Bundle, expected: any): {kind: FailureKind; strict: boolean; answer: any} {
  const strict = (()=>{ try { JSON.parse(bundle.final); return true; } catch { return false; } })();
  const data = extractJson(bundle.final);
  const claim = data?.claims?.find((c:any)=>c.id===expected.id);
  if (!claim) return {kind:'no_json', strict, answer:undefined};
  const answerOk = canonical(claim.answer) === canonical(expected.answer);
  const evidenceOk = expected.evidence.every((want:any)=>claim.evidence?.some((ref:any)=>
    ref.path===want.path && ref.line===want.line && ref.quote?.trim()===want.quote.trim()));
  if (answerOk && evidenceOk) return {kind: strict ? 'pass' : 'format_only', strict, answer:claim.answer};
  return {kind: answerOk ? 'bad_evidence' : 'wrong_answer', strict, answer:claim.answer};
}

function canonical(value:any){return typeof value==='string'?JSON.stringify(value):JSON.stringify(value);}

async function main() {
  const {values}=parseArgs({options:{dir:{type:'string'},suite:{type:'string'}}});
  const dir=path.resolve(String(values.dir));
  const suite=JSON.parse(await readFile(values.suite?path.resolve(String(values.suite)):fileURLToPath(new URL('../validation/suite.json',import.meta.url)),'utf8'));
  const expected=new Map<string,any>(suite.tasks.map((t:any)=>[t.id,t]));
  const rows:any[]=[];
  for (const file of await readdir(dir)) {
    if(!/\.(baseline|grounded)\.\d+\.json$/.test(file)) continue;
    const bundle:Bundle=JSON.parse(await readFile(path.join(dir,file),'utf8'));
    const task=file.split('.')[0];
    const spec=expected.get(task);
    if(!spec) continue;
    const result=classify(bundle,spec);
    rows.push({task,arm:file.includes('.grounded.')?'grounded':'baseline',file,...result});
  }
  const counts:Record<string,number>={};
  for(const r of rows) counts[r.kind]=(counts[r.kind]??0)+1;
  for(const arm of ['baseline','grounded']) {
    const subset=rows.filter(r=>r.arm===arm);
    const summary:Record<string,number>={};
    for(const r of subset) summary[r.kind]=(summary[r.kind]??0)+1;
    console.log(`${arm}: ${subset.length} samples -> ${JSON.stringify(summary)}`);
  }
  console.log(JSON.stringify({total:rows.length,counts},null,2));
  for(const r of rows.filter(r=>r.kind!=='pass')) console.log(`  ${r.kind.padEnd(13)} ${r.task} ${r.arm} answer=${JSON.stringify(r.answer)}`);
}
const invoked=process.argv[1] ?? '';
if(/[\\/]audit\.tsx?$/.test(invoked)) void main().catch(e=>{console.error(String(e));process.exitCode=3;});
