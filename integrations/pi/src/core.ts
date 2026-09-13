import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, lstat, readdir } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import path from 'node:path';
import { cause, reasonOf, type InconclusiveCode, type InconclusiveReason } from './reasons.ts';

export type Verdict = 'passed' | 'failed' | 'inconclusive' | 'error';
export class Incomplete extends Error {
  /** 成因码：稳定、可判定。 */
  code: InconclusiveCode;
  /** 给人看的具体信息。散文只放这里。 */
  detail: string;
  constructor(detail = '', code: InconclusiveCode = 'unknown') {
    super(detail);
    this.code = code;
    this.detail = detail;
  }
  /** 结构化成因。 */
  get cause(): InconclusiveReason {
    return cause(this.code, this.detail);
  }
}
export interface Step {
  kind: 'model_call' | 'tool_call'; name: string; input: any; output?: any;
  error?: string; duration_ms: number;
  /** Where this result came from. Absent on bundles written before snapshot mode existed. */
  source?: 'live' | 'recorded';
}
export interface Bundle {
  version: 1; id: string; parent?: string; started: string; ended: string;
  task: string; prompt: string; promptHash: string; provider: string; model: string;
  commit: string; piVersion: string; thinking: 'off'; mode: 'record' | 'reproduce' | 'regress';
  /** recorded: strict tape. snapshot: live read-only tools against the pinned checkout. */
  toolSource?: 'recorded' | 'snapshot';
  steps: Step[]; final: string; complete: boolean; reason?: string;
  /**
   * 结构化「无法判断」成因。新写入的包只有它；`reason` 仅保留旧包的原文，
   * 供向后兼容解析（见 reasonOf）。
   */
  cause?: InconclusiveReason;
  /** 旧包里的自由文本 reason 原文。 */
  legacyReason?: string;
  status: 'succeeded' | 'failed' | 'aborted'; verdict?: Verdict;
}
export const hash = (value: string) => createHash('sha256').update(value).digest('hex');
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
export function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export class Tape {
  private cursor = 0;
  constructor(private steps: Step[]) {}
  take(kind: Step['kind'], name?: string, input?: unknown): Step {
    const step = this.steps[this.cursor];
    if (!step || step.kind !== kind || (name !== undefined && step.name !== name) ||
      (input !== undefined && canonical(step.input) !== canonical(input))) {
      throw new Incomplete(
        `第 ${this.cursor + 1} 步找不到匹配的录制结果：${kind} ${name ?? ''}, args=${canonical(input ?? {})}`,
        'missing_recorded_response',
      );
    }
    this.cursor++;
    return clone(step);
  }
  finish() {
    if (this.cursor !== this.steps.length)
      throw new Incomplete(`父 Run 还有 ${this.steps.length - this.cursor} 个录制步骤没有被消费`, 'event_sequence_gap');
  }
  get consumed() { return this.cursor; }
}
/**
 * Resolve a path the way the operating system does, so that two spellings of one
 * directory compare equal. This has to be the OS resolver: an 8.3 short name
 * (RUNNER~1), a junction or a different case all name the same directory, and any
 * purely lexical comparison would treat them as different paths.
 */
function canonicalPath(target: string): string {
  try {
    return realpathSync.native(target);
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') throw new Error('Path does not exist');
    throw error;
  }
}
function leavesWorktree(relative: string): boolean {
  return relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative);
}
/**
 * Resolve the deepest existing ancestor. Containment has to be decided even when the
 * target itself is missing, otherwise a nonexistent path outside the worktree reports
 * "does not exist" instead of "outside", which reads like a lookup failure.
 */
function resolveExisting(target: string): { real: string; missing: number } {
  for (let current = target, missing = 0;; missing++) {
    try {
      return { real: realpathSync.native(current), missing };
    } catch (error) {
      if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') throw error;
    }
    const parent = path.dirname(current);
    if (parent === current) throw new Error('Path does not exist');
    current = parent;
  }
}
/**
 * The one canonical spelling of a worktree. The native tools are created with this
 * root and confined() resolves against the same value, so a tool can never be asked
 * about a path that its own root would consider outside.
 */
export function worktreeRoot(root: string): string {
  return canonicalPath(path.resolve(root));
}
export async function confined(root: string, requested = '.'): Promise<string> {
  if (requested.includes('\0') || requested.split(/[\\/]/).some(part => part.toLowerCase() === '.git'))
    throw new Error('Invalid repository path');
  const base = canonicalPath(path.resolve(root));
  const lexical = path.resolve(base, requested);
  const relative = path.relative(base, lexical);

  // Reject a link or junction anywhere along the path handed to a native tool. Only
  // meaningful while the request stays inside the worktree lexically; a request
  // spelled through an alias is decided by its resolved location below.
  if (!leavesWorktree(relative)) {
    let current = base;
    for (const part of relative.split(path.sep).filter(Boolean)) {
      current = path.join(current, part);
      let stat;
      try {
        stat = await lstat(current);
      } catch (error) {
        if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') throw new Error('Path does not exist');
        throw error;
      }
      if (stat.isSymbolicLink()) throw new Error('Symlink or junction is not allowed');
    }
  }

  // Containment is decided on the resolved location, never on the spelling. This is
  // what keeps a path inside the worktree: the value returned is always canonical,
  // so a short name or a junction cannot smuggle a target past the check below.
  const { real, missing } = resolveExisting(lexical);
  if (leavesWorktree(path.relative(base, real))) throw new Error('Path outside repository');
  if (missing) throw new Error('Path does not exist');
  return real;
}
// Scan trees before native recursive tools: internal links must not escape confinement.
export async function rejectLinks(root: string): Promise<void> {
  for (const entry of await readdir(root, { withFileTypes: true })) {
    if (entry.name === '.git') continue;
    const target = path.join(root, entry.name);
    const stat = await lstat(target);
    if (stat.isSymbolicLink()) throw new Error(`Symlink or junction is not allowed: ${entry.name}`);
    if (stat.isDirectory()) await rejectLinks(target);
  }
}
export function redact<T>(value: T): { value: T; changed: boolean } {
  let changed = false;
  const walk = (v: any, key = ''): any => {
    if (/^(authorization|api[_-]?key|access[_-]?token|password|secret)$/i.test(key)) {
      changed = true; return '[REDACTED]';
    }
    if (typeof v === 'string') return v.replace(/(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|AKIA[A-Z0-9]{16}|Bearer\s+[\w.\-]+|-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----)/g,
      () => { changed = true; return '[REDACTED]'; });
    if (Array.isArray(v)) return v.map(x => walk(x));
    if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k,x]) => [k,walk(x,k)]));
    return v;
  };
  return { value: walk(value), changed };
}
export async function save(file: string, bundle: Bundle): Promise<Bundle> {
  const clean = redact(bundle);
  if (clean.changed) {
    clean.value.complete = false;
    clean.value.cause = cause('redacted_replay_data', '回放包在落盘前命中脱敏规则，证据不再逐字可比');
    clean.value.verdict = 'inconclusive';
  }
  await mkdir(path.dirname(path.resolve(file)), { recursive: true });
  await writeFile(file, JSON.stringify(clean.value, null, 2), { mode: 0o600 });
  return clean.value;
}
export async function load(file: string): Promise<Bundle> {
  const b = JSON.parse(await readFile(file, 'utf8'));
  if (b.version !== 1 || !Array.isArray(b.steps) || typeof b.prompt !== 'string' || b.promptHash !== hash(b.prompt))
    throw new Incomplete('回放包版本或结构不受支持', 'incomplete_recording');
  if (!b.complete) {
    const parsed = reasonOf(b.cause ?? b.reason);
    throw new Incomplete(parsed.detail, parsed.code);
  }
  return b;
}
export function payload(b: Bundle) {
  const source = b.mode === 'reproduce' ? 'recorded' : 'live';
  const event = (type: string, seq: number, extra: any) => ({ id: `${b.id}-${seq}`, run_id: b.id, seq, type, started_at: b.started, ...extra });
  const events = [event('run_started', 1, { input: { task: b.task } }), ...b.steps.map((s,i) => event(s.kind, i+2, {
    name:s.name, input:s.kind === 'tool_call' ? {args:s.input} : s.input,
    output:s.kind === 'tool_call' ? {text:JSON.stringify(s.output),content:s.output} : {text: modelText(s.output), message:s.output, tool_calls:(s.output?.content ?? []).filter((c:any) => c.type === 'toolCall').map((c:any)=>({id:c.id,name:c.name,args:c.arguments}))},
    error:s.error ? {type:'ExecutionError',message:s.error} : null,
    duration_ms:s.duration_ms, side_effect:s.kind === 'tool_call' ? 'read' : null,
    tokens:s.kind === 'model_call' && s.output?.usage ? {input:s.output.usage.input,output:s.output.usage.output,total:s.output.usage.totalTokens} : null,
    cost_usd:s.kind === 'model_call' ? s.output?.usage?.cost?.total ?? null : null,
    effect_source:s.source ?? (b.mode === 'reproduce' ? 'recorded' : 'live'),
    attributes:{native_usage:s.output?.usage ?? null},
  }))];
  if (b.reason) events.push(event('error',events.length+1,{error:{type:b.verdict==='inconclusive'?'IncompleteReplay':'ExecutionError',message:b.reason}}));
  const cause = b.cause ?? (b.reason ? reasonOf(b.reason) : undefined);
  events.push(event('run_finished',events.length+1,{started_at:b.ended,output:{result:b.final,status:b.status}}));
  return {protocol_version:1,sdk:{name:'msee-pi',version:'0.1.0'},run:{id:b.id,agent_name:'pi-code-investigator',status:b.status,model:`${b.provider}/${b.model}`,started_at:b.started,ended_at:b.ended,parent_run_id:b.parent ?? null,replay_from_seq:b.parent ? 1 : null,prompt_version:b.promptHash,labels:{runtime:'pi'},metadata:{runtime:'pi',commit:b.commit,pi_version:b.piVersion,afr_replay:{complete:b.complete,reason:cause,verdict:b.verdict}}},events};
}
export function modelText(message:any): string { return (message?.content ?? []).filter((c:any)=>c.type==='text').map((c:any)=>c.text).join('\n'); }
export async function upload(b:Bundle, endpoint:string) {
  try {
    const response = await fetch(`${endpoint.replace(/\/$/,'')}/v1/ingest`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload(b)),signal:AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return {uploaded:true};
  } catch (e) { return {uploaded:false,error:String(e)}; }
}
export function fresh(task:string,prompt:string,provider:string,model:string,commit:string): Bundle {
  return {version:1,id:randomUUID().replaceAll('-',''),started:new Date().toISOString(),ended:new Date().toISOString(),task,prompt,promptHash:hash(prompt),provider,model,commit,piVersion:'0.85.1',thinking:'off',mode:'record',steps:[],final:'',complete:true,status:'succeeded'};
}
