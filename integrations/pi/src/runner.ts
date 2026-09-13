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
import { clone, canonical, confined, rejectLinks, Incomplete, Tape, modelText, type Bundle, type Step } from './core.ts';

export interface RunOptions {
  bundle: Bundle; parent?: Bundle; root?: string; authPath?: string; modelsPath?: string;
  maxModels?: number; maxTools?: number; timeoutMs?: number;
  // Dependency injection for offline SDK contract tests; never exposed by the CLI.
  stream?: (...args: any[]) => any;
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
  const previousOffline=process.env.PI_OFFLINE;
  process.env.PI_OFFLINE='1'; // Prevent native tool bootstrap downloads; model transport remains explicit.
  const all = new Tape(parent?.steps ?? []);
  const toolsTape = new Tape((parent?.steps ?? []).filter(s=>s.kind==='tool_call'));
  let fatal: Error | undefined;
  let models = 0, tools = 0;
  let session: Awaited<ReturnType<typeof createAgentSession>>['session'] | undefined;
  const aborter = new AbortController();
  const timer = setTimeout(()=>{
    fatal = new Error('time_budget_exceeded'); aborter.abort(); void session?.abort();
  }, options.timeoutMs ?? 600000);
  const guard = () => { if (fatal) throw fatal; if (aborter.signal.aborted) throw new Error('cancelled'); };
  const interrupt = () => { fatal = new Error('cancelled'); aborter.abort(); void session?.abort(); };
  process.once('SIGINT',interrupt);
  try {
    if (parent && (!parent.complete || parent.piVersion !== b.piVersion)) throw new Incomplete('incomplete_or_incompatible_recording');
    if (!replay && !options.root) throw new Error('record requires isolated repository');
    // An empty runtime directory is used for replay: no access to the recorded checkout.
    const cwd = replay ? scratch : options.root!;
    if (!replay) await rejectLinks(cwd);
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
        if (replay) {
          const step = b.mode === 'reproduce' ? all.take('tool_call',tool.name,input) : toolsTape.take('tool_call',tool.name,input);
          b.steps.push(step);
          if (step.error) throw new Error(step.error);
          return clone(step.output);
        }
        const requested = input.path ?? '.';
        if (typeof requested !== 'string' || requested.split(/[\\/]/).includes('.git')) throw new Error('Invalid repository path');
        const target = await confined(cwd,requested);
        await rejectLinks(cwd);
        const executeArgs=[...args];executeArgs[1]={...input,path:target};
        const output = await (tool.execute as any)(...executeArgs);
        b.steps.push({kind:'tool_call',name:tool.name,input,output:clone(output),duration_ms:performance.now()-start});
        return output;
      } catch(e) {
        if (e instanceof Incomplete) { fatal=e; aborter.abort(); }
        if (!replay) b.steps.push({kind:'tool_call',name:tool.name,input,error:e instanceof Error?e.message:String(e),duration_ms:performance.now()-start});
        throw e;
      }
    }}));
    const live = options.stream ?? session.agent.streamFunction;
    session.agent.streamFunction = ((model:any,context:any,streamOptions:any) => {
      guard();
      // Pi appends the physical cwd to its base prompt. Keep the model-visible prompt
      // explicit and stable across the isolated checkout and checkout-free replay.
      context = {...context,systemPrompt:b.prompt};
      if (++models > (options.maxModels ?? 20)) { fatal = new Error('model_budget_exceeded'); throw fatal; }
      const input = modelInput(context);
      const output = createAssistantMessageEventStream();
      const start = performance.now();
      void (async()=>{
        try {
          if (b.mode === 'reproduce') {
            const step=all.take('model_call');
            if (canonical(step.input)!==canonical(input)) throw new Incomplete('model_context_changed');
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
              b.steps.push({kind:'model_call',name:`${b.provider}/${b.model}`,input,output:clone(message),duration_ms:performance.now()-start});
              recorded=true;
            }
            output.push(event);
          }
          const message=await stream.result();
          if(!recorded) b.steps.push({kind:'model_call',name:`${b.provider}/${b.model}`,input,output:clone(message),duration_ms:performance.now()-start});
          output.end(message);
        } catch(e) {
          fatal=e instanceof Error ? e : new Error(String(e));
          if (b.mode!=='reproduce') b.steps.push({kind:'model_call',name:`${b.provider}/${b.model}`,input,error:fatal.message,duration_ms:performance.now()-start});
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
    if (last?.stopReason==='length') throw new Incomplete('model_output_truncated');
    if (b.mode==='reproduce') {
      all.finish();
      if (b.final!==parent!.final) throw new Incomplete('final_output_changed');
    }
  } catch(e) {
    const err=fatal ?? (e instanceof Error ? e : new Error(String(e)));
    b.reason=err.message;
    b.complete=!(err instanceof Incomplete);
    b.verdict=err instanceof Incomplete ? 'inconclusive' : 'error';
    b.status=/cancelled|budget_exceeded/.test(err.message) ? 'aborted':'failed';
  } finally {
    clearTimeout(timer);process.off('SIGINT',interrupt);
    if(previousOffline===undefined)delete process.env.PI_OFFLINE;else process.env.PI_OFFLINE=previousOffline;
    session?.dispose();
    b.ended=new Date().toISOString();
    // Only this uniquely generated, verified temp child belongs to this invocation.
    if (path.dirname(scratch)===os.tmpdir() && path.basename(scratch).startsWith('msee-pi-runtime-')) await rm(scratch,{recursive:true,force:true});
  }
  return b;
}
