import { canonical, type Bundle, type Verdict } from './core.ts';

export interface Assertion { type: string; value?: any; tool?: string; }
export interface CaseFile { version: 1; bundle: string; bundleHash: string; assertions: Assertion[]; }
export function validateAssertions(value: unknown): asserts value is Assertion[] {
  if (!Array.isArray(value) || !value.length) throw new Error('A case requires nonempty assertions');
  for (const a of value) {
    if (!a || !['no_error','final_output_contains','final_output_not_contains','final_output_matches','tool_called','tool_not_called','tool_sequence_equals','max_tool_calls','json_claim'].includes(a.type)) throw new Error('Unsupported assertion');
    if (a.type === 'final_output_matches') new RegExp(a.value,'s');
    if (a.type === 'json_claim' && (!a.value?.id || !Array.isArray(a.value.evidence) || !a.value.evidence.length)) throw new Error('json_claim requires id, answer and evidence');
  }
}
export function evaluate(b:Bundle, assertions:Assertion[]): {verdict:Verdict; results:{type:string;passed:boolean}[]} {
  validateAssertions(assertions);
  if (!b.complete) return {verdict:'inconclusive',results:[]};
  if (b.status!=='succeeded') return {verdict:'error',results:[]};
  const tools=b.steps.filter(s=>s.kind==='tool_call');
  const results=assertions.map(a=>{
    let passed=false;
    switch(a.type) {
      case 'no_error': passed=!b.steps.some(s=>s.error || s.output?.stopReason==='error');break;
      case 'final_output_contains': passed=b.final.toLowerCase().includes(String(a.value).toLowerCase());break;
      case 'final_output_not_contains': passed=!b.final.toLowerCase().includes(String(a.value).toLowerCase());break;
      case 'final_output_matches': passed=new RegExp(a.value,'s').test(b.final);break;
      case 'tool_called': passed=tools.some(s=>s.name===a.tool);break;
      case 'tool_not_called': passed=!tools.some(s=>s.name===a.tool);break;
      case 'tool_sequence_equals': passed=canonical(tools.map(s=>s.name))===canonical(a.value);break;
      case 'max_tool_calls': passed=tools.length<=a.value;break;
      case 'json_claim': {
        try {
          const data=JSON.parse(b.final.replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,''));
          const claim=data.claims?.find((c:any)=>c.id===a.value.id);
          passed=claim && canonical(claim.answer)===canonical(a.value.answer) && a.value.evidence.every((expected:any)=>
            claim.evidence?.some((ref:any)=>ref.path===expected.path && Number.isInteger(ref.line) &&
              ref.line===expected.line && typeof ref.quote==='string' && ref.quote.trim()===expected.quote.trim()));
        } catch { passed=false; }
        break;
      }
    }
    return {type:a.type,passed:!!passed};
  });
  return {verdict:results.every(r=>r.passed)?'passed':'failed',results};
}
export const exitCode = (verdict:Verdict) => ({passed:0,failed:1,inconclusive:2,error:3})[verdict];
