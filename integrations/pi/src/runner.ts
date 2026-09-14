import { mkdtemp, rm, stat, readFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import os from 'node:os';
import path from 'node:path';
import {
  createAgentSession, createExtensionRuntime, createReadTool, createGrepTool, createFindTool, createLsTool,
  ModelRuntime, SessionManager, SettingsManager, type ResourceLoader,
} from '@earendil-works/pi-coding-agent';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { clone, canonical, confined, rejectLinks, worktreeRoot, Incomplete, Tape, modelText, type Bundle, type Step } from './core.ts';
import { BudgetLedger, isDeclared, stopDetail, type BudgetSpec } from './budget.ts';
import { cause } from './reasons.ts';

export interface RunOptions {
  bundle: Bundle; parent?: Bundle; root?: string; authPath?: string; modelsPath?: string;
  maxModels?: number; maxTools?: number; timeoutMs?: number;
  /**
   * 这次执行的声明上限（`max_model_calls` / `max_cost_usd`，与 Python 侧的
   * `ReplayBudget` 同名同义）。批量套件会把「整批剩下的额度」分给每一格传进来。
   * 不声明时行为与加这套能力之前逐字一致。
   */
  budget?: BudgetSpec | null;
  /** Regress only. `snapshot` re-runs read-only tools against the pinned checkout instead of the tape. */
  toolSource?: 'recorded' | 'snapshot';
  // Dependency injection for offline SDK contract tests; never exposed by the CLI.
  stream?: (...args: any[]) => any;
}

/**
 * 模型配置里的凭证引用是否是活的。缺凭证要当场说清楚，不能让它退化成一个「跑完了、
 * 但结论是在没有真实模型的情况下得出的」数字——那正是本项目最不能接受的那种结论。
 */
export async function assertProviderCredentials(modelsPath: string): Promise<void> {
  const config = JSON.parse(await readFile(modelsPath, 'utf8'));
  const missing: string[] = [];
  for (const [name, provider] of Object.entries<any>(config.providers ?? {})) {
    const apiKey = provider?.apiKey;
    if (typeof apiKey !== 'string' || !apiKey.startsWith('$')) continue;
    if (!process.env[apiKey.slice(1)]) missing.push(`${name}: ${apiKey}`);
  }
  if (missing.length) {
    throw new Error(
      `缺少凭证，真实调用没有开始（未设置的环境变量：${missing.join('、')}）。` +
      '请先把网关密钥放进进程环境变量再运行；不要把凭证明文写进仓库、配置或测试。',
    );
  }
}

/**
 * 把一个 provider/model 解析成实际会被调用的模型定义（**不发起任何调用**）。
 * 免费的自检：模型不在配置里、配置写错、provider 名字打错，都在花钱之前暴露出来。
 */
export async function resolveModelInfo(options: {authPath?: string; modelsPath?: string; provider: string; model: string}) {
  const scratch = await mkdtemp(path.join(os.tmpdir(),'msee-pi-resolve-'));
  try {
    const runtime = await ModelRuntime.create({
      authPath: options.authPath ?? path.join(scratch,'auth.json'),
      modelsPath: options.modelsPath ?? null,
      modelsStorePath: path.join(scratch,'models-store.json'),
      refreshOnCreate: false, allowModelNetwork: false,
    });
    const model = runtime.getModel(options.provider, options.model) as any;
    if (!model) return undefined;
    return {id:model.id,provider:model.provider,api:model.api,baseUrl:model.baseUrl,
      cost:model.cost??null,contextWindow:model.contextWindow??null,maxTokens:model.maxTokens??null};
  } finally {
    if (path.dirname(scratch)===os.tmpdir() && path.basename(scratch).startsWith('msee-pi-resolve-'))
      await rm(scratch,{recursive:true,force:true});
  }
}

/**
 * pi resolves `!command` apiKey/header values by executing them, and environment
 * interpolation inside larger literals. This runner allows $ENV / literal values only,
 * so a config file cannot turn replay into arbitrary command execution.
 */
export async function assertNoCommandExecution(modelsPath: string): Promise<void> {
  const config = JSON.parse(await readFile(modelsPath, 'utf8'));
  for (const provider of Object.values<any>(config.providers ?? {})) {
    const values = [provider.apiKey, ...Object.values<any>(provider.headers ?? {}), ...(provider.models ?? []).map((m:any)=>m.apiKey)];
    if (values.some((value) => typeof value === 'string' && value.startsWith('!'))) {
      throw new Error('models.json: command execution is not allowed for apiKey/headers');
    }
  }
}
function loader(prompt:string): ResourceLoader {
  return {
    getExtensions:()=>({extensions:[],errors:[],runtime:createExtensionRuntime()}),
    getSkills:()=>({skills:[],diagnostics:[]}),getPrompts:()=>({prompts:[],diagnostics:[]}),
    getThemes:()=>({themes:[],diagnostics:[]}),getAgentsFiles:()=>({agentsFiles:[]}),
    getSystemPrompt:()=>prompt,getSystemPromptSource:()=>undefined,getAppendSystemPrompt:()=>[],
    getAppendSystemPromptSources:()=>[],extendResources:()=>{},reload:async()=>{},
  };
}
const dummy = { id:'recorded',name:'Recorded',api:'openai-completions',provider:'recorded',baseUrl:'http://127.0.0.1:1',
  reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:1000000,maxTokens:8192 };
function modelInput(context:any) {
  const c = clone(context);
  // Runtime timestamps are not model-visible input. All content and tool arguments remain exact.
  for (const m of c.messages ?? []) delete m.timestamp;
  return c;
}
export async function run(options: RunOptions): Promise<Bundle> {
  const {bundle:b,parent} = options;
  const scratch = await mkdtemp(path.join(os.tmpdir(),'msee-pi-runtime-'));
  const replay = b.mode !== 'record';
  // A frozen commit plus a read-only tool set means live tool results are still comparable:
  // the evidence cannot drift, so the model may explore freely instead of replaying a tape.
  const toolSource = b.toolSource ?? options.toolSource ?? 'recorded';
  const liveTools = b.mode === 'record' || (b.mode === 'regress' && toolSource === 'snapshot');
  const previousOffline=process.env.PI_OFFLINE;
  process.env.PI_OFFLINE='1'; // Prevent native tool bootstrap downloads; model transport remains explicit.
  const all = new Tape(parent?.steps ?? []);
  const toolsTape = new Tape((parent?.steps ?? []).filter(s=>s.kind==='tool_call'));
  let fatal: Error | undefined;
  let models = 0, tools = 0;
  // 声明的上限只约束真实发生的模型调用；读取录制结果的步骤既不花成本也不占次数。
  // 声明之后，调用次数这一维取「声明值」与「--max-models 安全上限」里更紧的那个，
  // 记账只走账本一处；不声明时下面是原来的那条老路，一字未改。
  const declared = isDeclared(options.budget) ? (options.budget as BudgetSpec) : null;
  const effective: BudgetSpec | null = declared
    ? {
        max_model_calls: Math.min(
          declared.max_model_calls ?? Number.POSITIVE_INFINITY,
          options.maxModels ?? 20,
        ),
        max_cost_usd: declared.max_cost_usd ?? null,
      }
    : null;
  const ledger = new BudgetLedger(effective);
  /** 一次已完成调用的成本；拿不到用量就是「未知」，不是 0。 */
  const costOf = (message: any): number | null => {
    // 失败/中止的那次调用没有可用的用量记录：按「未知」记，不按 0 记（0 会把一次
    // 真实的支出说成没花钱）。
    if (message?.stopReason === 'error' || message?.stopReason === 'aborted') return null;
    const total = message?.usage?.cost?.total;
    return typeof total === 'number' && Number.isFinite(total) ? total : null;
  };
  let session: Awaited<ReturnType<typeof createAgentSession>>['session'] | undefined;
  const aborter = new AbortController();
  const timer = setTimeout(()=>{
    fatal = new Error('time_budget_exceeded'); aborter.abort(); void session?.abort();
  }, options.timeoutMs ?? 600000);
  const guard = () => { if (fatal) throw fatal; if (aborter.signal.aborted) throw new Error('cancelled'); };
  const interrupt = () => { fatal = new Error('cancelled'); aborter.abort(); void session?.abort(); };
  process.once('SIGINT',interrupt);
  try {
    if (parent && (!parent.complete || parent.piVersion !== b.piVersion))
      throw new Incomplete('父 Run 不完整，或它与当前 pi 版本不兼容', 'incomplete_recording');
    if (liveTools && !options.root) throw new Error(`${b.mode} with toolSource=${toolSource} requires an isolated repository`);
    // A reproduce run uses an empty runtime directory: it must not read the recorded checkout.
    // One canonical root for the tools and for confinement: a short name or junction
    // must not make the tool root and the resolved path disagree.
    const cwd = liveTools ? worktreeRoot(options.root!) : scratch;
    if (liveTools) await rejectLinks(cwd);
    if (options.modelsPath) await assertNoCommandExecution(options.modelsPath);
    const runtime = await ModelRuntime.create({authPath: options.authPath ?? path.join(scratch,'auth.json'),
      modelsPath:options.modelsPath ?? null,modelsStorePath:path.join(scratch,'models-store.json'),
      refreshOnCreate:false,allowModelNetwork:false});
    if (b.mode === 'reproduce' || options.stream) runtime.registerProvider('recorded', {
      api:'openai-completions',baseUrl:dummy.baseUrl,apiKey:'offline-not-a-credential',models:[dummy as any],
    });
    const model = b.mode === 'reproduce' || options.stream ? dummy : runtime.getModel(b.provider,b.model);
    if (!model) throw new Error(`Unknown explicit model ${b.provider}/${b.model}`);
    const created = await createAgentSession({cwd,agentDir:scratch,modelRuntime:runtime,model:model as any,
      thinkingLevel:'off',tools:['read','grep','find','ls'],resourceLoader:loader(b.prompt),
      settingsManager:SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false,maxRetries:0,provider:{maxRetries:0}},images:{blockImages:true}}),
      sessionManager:SessionManager.inMemory(cwd)});
    session = created.session;
    session.agent.state.systemPrompt = b.prompt;
    session.agent.toolExecution = 'sequential';
    const find=createFindTool(cwd,{operations:{
      exists:async p=>{try{return (await stat(p)).isDirectory();}catch{return false;}},
      glob:async(pattern,search,opts)=>{
        const {stdout}=await promisify(execFile)('git',['-C',cwd,'ls-files','-z'],{windowsHide:true,maxBuffer:8*1024*1024});
        return stdout.split('\0').filter(Boolean).map(f=>path.join(cwd,f)).filter(f=>{
          const rel=path.relative(search,f).split(path.sep).join('/');
          return !rel.startsWith('../') && !path.isAbsolute(rel) &&
            !rel.split('/').includes('node_modules') && path.posix.matchesGlob(rel,pattern.includes('/')?pattern:`**/${pattern}`);
        }).slice(0,opts.limit);
      },
    }});
    find.description='Find tracked repository files by glob, relative to the selected directory. Results are bounded by limit.';
    const nativeTools = [createReadTool(cwd),createGrepTool(cwd),find,createLsTool(cwd)];
    session.agent.state.tools = nativeTools.map(tool => ({...tool, execute: async (...args:any[]) => {
      guard();
      if (++tools > (options.maxTools ?? 60)) { fatal = new Error('tool_budget_exceeded'); throw fatal; }
      const input = clone(args[1]);
      const start = performance.now();
      try {
        if (!liveTools) {
          const step = b.mode === 'reproduce' ? all.take('tool_call',tool.name,input) : toolsTape.take('tool_call',tool.name,input);
          step.source = 'recorded';
          b.steps.push(step);
          if (step.error) throw new Error(step.error);
          return clone(step.output);
        }
        const requested = input.path ?? '.';
        // The .git policy and path canonicalization both live in confined(), so every
        // tool path goes through one decision instead of two that can disagree.
        if (typeof requested !== 'string') throw new Error('Invalid repository path');
        const target = await confined(cwd,requested);
        await rejectLinks(cwd);
        const executeArgs=[...args];executeArgs[1]={...input,path:target};
        const output = await (tool.execute as any)(...executeArgs);
        b.steps.push({kind:'tool_call',name:tool.name,input,output:clone(output),duration_ms:performance.now()-start,source:'live'});
        return output;
      } catch(e) {
        if (e instanceof Incomplete) { fatal=e; aborter.abort(); }
        if (liveTools) b.steps.push({kind:'tool_call',name:tool.name,input,error:e instanceof Error?e.message:String(e),duration_ms:performance.now()-start,source:'live'});
        throw e;
      }
    }}));
    const live = options.stream ?? session.agent.streamFunction;
    session.agent.streamFunction = ((model:any,context:any,streamOptions:any) => {
      guard();
      // Pi appends the physical cwd to its base prompt. Keep the model-visible prompt
      // explicit and stable across the isolated checkout and checkout-free replay.
      context = {...context,systemPrompt:b.prompt};
      models += 1;
      if (effective) {
        // 步边界判定：这一步真要发出去之前先看账。已经发出的那次调用允许完成并记账，
        // 这里不做预测性中断。触顶是「因上限而停」，因此落预算语义而不是执行错误。
        const stoppedBy = ledger.exceededBy();
        if (stoppedBy !== null) {
          ledger.stoppedBy = stoppedBy;
          fatal = new Incomplete(stopDetail(ledger.usage(), stoppedBy), 'budget_exceeded');
          aborter.abort();
          throw fatal;
        }
      } else if (models > (options.maxModels ?? 20)) {
        fatal = new Error('model_budget_exceeded');
        throw fatal;
      }
      const input = modelInput(context);
      const output = createAssistantMessageEventStream();
      const start = performance.now();
      void (async()=>{
        try {
          if (b.mode === 'reproduce') {
            const step=all.take('model_call');
            if (canonical(step.input)!==canonical(input))
              throw new Incomplete('这次回放的模型上下文与录制不一致，无法逐字复现', 'model_context_changed');
            step.source='recorded';
            b.steps.push(step);
            if (!step.output) throw new Error(step.error ?? 'missing_model_response');
            const m=clone(step.output);
            if (m.stopReason==='error'||m.stopReason==='aborted') output.push({type:'error',reason:m.stopReason,error:m});
            else output.push({type:'done',reason:m.stopReason,message:m});
            output.end(m); return;
          }
          const stream=await live(model,context,{...streamOptions,signal:AbortSignal.any([aborter.signal,...(streamOptions?.signal ? [streamOptions.signal] : [])])});
          let recorded=false;
          for await (const event of stream) {
            if (event.type==='done'||event.type==='error') {
              const message=event.type==='done'?event.message:event.error;
              b.steps.push({kind:'model_call',name:`${b.provider}/${b.model}`,input,output:clone(message),duration_ms:performance.now()-start,source:'live'});
              if (effective) ledger.noteModelCall(costOf(message));
              recorded=true;
            }
            output.push(event);
          }
          const message=await stream.result();
          if(!recorded) {
            b.steps.push({kind:'model_call',name:`${b.provider}/${b.model}`,input,output:clone(message),duration_ms:performance.now()-start,source:'live'});
            if (effective) ledger.noteModelCall(costOf(message));
          }
          output.end(message);
        } catch(e) {
          fatal=e instanceof Error ? e : new Error(String(e));
          if (b.mode!=='reproduce') b.steps.push({kind:'model_call',name:`${b.provider}/${b.model}`,input,error:fatal.message,duration_ms:performance.now()-start,source:'live'});
          const message:any={role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,
            usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:'error',errorMessage:String(e),timestamp:Date.now()};
          output.push({type:'error',reason:'error',error:message});output.end(message);
        }
      })();
      return output;
    }) as any;
    await session.prompt(b.task);
    guard();
    const last = [...session.agent.state.messages].reverse().find((m:any)=>m.role==='assistant') as any;
    b.final=modelText(last);
    if (last?.stopReason==='error'||last?.stopReason==='aborted') throw new Error(last.errorMessage ?? last.stopReason);
    if (last?.stopReason==='length') throw new Incomplete('模型输出被长度上限截断', 'truncated_context');
    if (b.mode==='reproduce') {
      all.finish();
      if (b.final!==parent!.final) throw new Incomplete('复现出来的结论与录制不同', 'final_output_changed');
    }
    if (b.mode==='regress' && toolSource==='recorded') toolsTape.finish();
  } catch(e) {
    const err=fatal ?? (e instanceof Error ? e : new Error(String(e)));
    // reason 保持可读的文本形态（旧读者照常能用）；判定一律走结构化的 cause。
    b.reason=err.message;
    b.complete=!(err instanceof Incomplete);
    b.verdict=err instanceof Incomplete ? 'inconclusive' : 'error';
    // 成因结构化：码与说明分开，不再把整句散文塞进 reason 充当码。
    b.cause=err instanceof Incomplete ? err.cause : cause('unknown', err.message);
    // 「中止」的判据看**成因码**，不再只认字符串里有没有 budget_exceeded：声明的预算触顶
    // 走的是结构化成因，说明文本是给人看的中文，文本里本来就不该出现机器值。
    const budgetStop = err instanceof Incomplete
      ? err.cause.code === 'budget_exceeded'
      : /cancelled|budget_exceeded/.test(err.message);
    b.status=budgetStop ? 'aborted':'failed';
  } finally {
    clearTimeout(timer);process.off('SIGINT',interrupt);
    if(previousOffline===undefined)delete process.env.PI_OFFLINE;else process.env.PI_OFFLINE=previousOffline;
    session?.dispose();
    // 记账与结论一起返回：只有声明了上限的执行才有这个对象。
    if (effective) b.budget = ledger.usage();
    b.toolSource = b.mode === 'reproduce' ? 'recorded' : toolSource;
    b.ended=new Date().toISOString();
    // Only this uniquely generated, verified temp child belongs to this invocation.
    if (path.dirname(scratch)===os.tmpdir() && path.basename(scratch).startsWith('msee-pi-runtime-')) await rm(scratch,{recursive:true,force:true});
  }
  return b;
}
