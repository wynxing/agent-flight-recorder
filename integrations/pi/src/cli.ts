import { parseArgs } from 'node:util';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fresh, hash, load, save, upload, Incomplete } from './core.ts';
import { run } from './runner.ts';
import { snapshot } from './workspace.ts';
import { evaluate, validateAssertions, exitCode, type CaseFile } from './cases.ts';

const strings=['repo','commit','task','prompt','provider','model','bundle','mode','out','assertions','case','endpoint','auth-path','models-path','max-models','max-tools','timeout-ms','tool-source'];
const {values,positionals}=parseArgs({allowPositionals:true,options:Object.fromEntries(strings.map(k=>[k,{type:'string' as const}]))});
const get=(name:string)=>values[name] as string|undefined;
const required=(name:string)=>{const v=get(name);if(!v)throw new Error(`--${name} is required`);return v;};
const number=(name:string,fallback:number)=>{const n=Number(get(name)??fallback);if(!Number.isSafeInteger(n)||n<=0)throw new Error(`Invalid --${name}`);return n;};
async function main() {
  const command=positionals.join(' ');
  if (!['record','replay','case create','case run'].includes(command)) throw new Error('Commands: record | replay | case create | case run. See README.md for arguments.');
  const out=path.resolve(required('out'));
  if(command==='case create') {
    const source=path.resolve(required('bundle'));
    await load(source);
    const assertions=JSON.parse(await readFile(required('assertions'),'utf8'));validateAssertions(assertions);
    const c:CaseFile={version:1,bundle:path.relative(path.dirname(out),source),bundleHash:hash(await readFile(source,'utf8')),assertions};
    await mkdir(path.dirname(out),{recursive:true});await writeFile(out,JSON.stringify(c,null,2));
    console.log(JSON.stringify({case:out}));return;
  }
  let caseFile:CaseFile|undefined;
  let bundlePath=get('bundle');
  if(command==='case run') {
    const file=path.resolve(required('case'));
    caseFile=JSON.parse(await readFile(file,'utf8'));
    if(caseFile?.version!==1)throw new Error('Unsupported case version');
    validateAssertions(caseFile.assertions);
    bundlePath=path.resolve(path.dirname(file),caseFile.bundle);
    if(hash(await readFile(bundlePath,'utf8'))!==caseFile.bundleHash)throw new Incomplete('Case source bundle changed');
  }
  const parent=command==='record'?undefined:await load(bundlePath??required('bundle'));
  const mode=command==='record'?'record':get('mode')??'regress';
  if(!['record','reproduce','regress'].includes(mode) || (command!=='record'&&mode==='record'))throw new Error('Invalid mode');
  if(mode==='reproduce'&&(get('prompt')||get('provider')||get('model')))throw new Error('Reproduce does not accept model or prompt overrides');
  if((get('provider')&&!get('model'))||(!get('provider')&&get('model')))throw new Error('Provide --provider and --model together');
  const task=parent?.task??await readFile(required('task'),'utf8');
  const prompt=get('prompt')?await readFile(required('prompt'),'utf8'):parent?.prompt??await readFile(required('prompt'),'utf8');
  const provider=get('provider')??parent?.provider??required('provider');
  const model=get('model')??parent?.model??required('model');
  const toolSource=get('tool-source')??'recorded';
  if(!['recorded','snapshot'].includes(toolSource))throw new Error('Invalid --tool-source');
  if(toolSource==='snapshot'&&!['record','regress'].includes(mode))throw new Error('--tool-source snapshot applies to record or regress only');
  const needsCheckout=mode==='record'||(mode==='regress'&&toolSource==='snapshot');
  const checkout=needsCheckout?await snapshot(path.resolve(required('repo')),get('commit')??parent?.commit??'HEAD'):undefined;
  const b=fresh(task,prompt,provider,model,checkout?.commit??parent!.commit);
  b.mode=mode as typeof b.mode;b.parent=parent?.id;
  let evaluation:ReturnType<typeof evaluate>|undefined;
  try {
    await run({bundle:b,parent,root:checkout?.root,authPath:get('auth-path'),modelsPath:get('models-path'),
      toolSource:toolSource as 'recorded'|'snapshot',
      maxModels:number('max-models',20),maxTools:number('max-tools',60),timeoutMs:number('timeout-ms',600000)});
    if(caseFile) {evaluation=evaluate(b,caseFile.assertions);b.verdict=evaluation.verdict;}
  } finally {
    try {await checkout?.close();} catch(e) { b.status='failed';b.verdict='error';b.reason=String(e); }
    const stored=await save(out,b);
    const transport=await upload(stored,get('endpoint')??'http://127.0.0.1:7710');
    const verdict=stored.verdict??(stored.complete&&stored.status==='succeeded'?'passed':stored.complete?'error':'inconclusive');
    console.log(JSON.stringify({bundle:out,run_id:stored.id,verdict,toolSource:stored.toolSource,reason:stored.reason,results:evaluation?.results,...transport}));
    process.exitCode=exitCode(verdict);
  }
}
main().catch(e=>{console.log(JSON.stringify({verdict:e instanceof Incomplete?'inconclusive':'error',reason:String(e)}));process.exitCode=e instanceof Incomplete?2:3;});
