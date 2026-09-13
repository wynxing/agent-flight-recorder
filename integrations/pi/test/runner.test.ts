import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, rm, symlink, mkdir } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { fresh, clone, Tape, Incomplete, confined, rejectLinks, worktreeRoot, redact, payload, save, load } from '../src/core.ts';
import { run, assertNoCommandExecution } from '../src/runner.ts';
import { evaluate } from '../src/cases.ts';
import { extractJson, classify } from '../src/audit.ts';

function response(content:any[],stopReason='stop') {
  const m:any={role:'assistant',content,api:'openai-completions',provider:'test',model:'test',
    usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason,timestamp:123};
  const s=createAssistantMessageEventStream();
  queueMicrotask(()=>{s.push({type:'done',reason:stopReason as any,message:m});s.end(m);});
  return s;
}
test('real pi SDK records read, reproduces without checkout or provider, and stops on changed args',async()=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'msee-test-'));
  try {
    await writeFile(path.join(root,'example.txt'),'repository evidence');
    const b=fresh('Investigate','Use read.','test','test','abc');let calls=0;
    await run({bundle:b,root,stream:()=> ++calls===1
      ? response([{type:'toolCall',id:'call-1',name:'read',arguments:{path:'example.txt'}}],'toolUse')
      : response([{type:'text',text:'Evidence found'}])});
    assert.equal(b.status,'succeeded',b.reason);
    assert.deepEqual(b.steps.map(s=>s.kind),['model_call','tool_call','model_call']);
    assert.match(JSON.stringify(b.steps[1].output),/repository evidence/);
    const replay=fresh(b.task,b.prompt,b.provider,b.model,b.commit);replay.mode='reproduce';
    await run({bundle:replay,parent:b,stream:()=>{throw new Error('provider must not run');}});
    assert.equal(replay.status,'succeeded',replay.reason);
    assert.equal(replay.final,b.final);
    assert.deepEqual(replay.steps.map(s=>({...s,source:undefined})),b.steps.map(s=>({...s,source:undefined})));
    assert.deepEqual(replay.steps.map(s=>s.source),['recorded','recorded','recorded']);
    assert.equal(payload(replay).events[2].effect_source,'recorded');
    const regress=fresh(b.task,b.prompt,b.provider,b.model,b.commit);regress.mode='regress';
    await run({bundle:regress,parent:b,stream:()=>response([{type:'toolCall',id:'new',name:'read',arguments:{path:'other.txt'}}],'toolUse')});
    assert.equal(regress.complete,false);
    assert.equal(regress.verdict,'inconclusive');
    assert.equal(regress.cause?.code,'missing_recorded_response');
    assert.equal(evaluate(regress,[{type:'final_output_not_contains',value:'unrelated'}]).verdict,'inconclusive');
    const file=path.join(root,'bundle.json');await save(file,b);assert.equal((await load(file)).id,b.id);
    const protocol=payload(replay);assert.equal(protocol.protocol_version,1);assert.equal(protocol.events.length,5);
  } finally {await rm(root,{recursive:true,force:true});}
});
test('exact tape consumes repeated arguments once and rejects early end',()=>{
  const step:any={kind:'tool_call',name:'read',input:{path:'a'},output:1,duration_ms:0};
  const tape=new Tape([step,{...step,output:2}]);
  assert.equal(tape.take('tool_call','read',{path:'a'}).output,1);
  assert.throws(()=>tape.finish(),Incomplete);
  assert.equal(tape.take('tool_call','read',{path:'a'}).output,2);tape.finish();
  assert.throws(()=>tape.take('tool_call','read',{path:'a'}),Incomplete);
});
test('confinement rejects escapes and answers on the resolved location',async()=>{
  // os.tmpdir() can hand back an 8.3 short name (RUNNER~1 on a GitHub runner). Nothing
  // below asserts a spelling: it asserts where the path really is, so a machine that
  // spells the same directory differently cannot change the verdict.
  const root=await mkdtemp(path.join(os.tmpdir(),'msee-path-'));
  const outside=await mkdtemp(path.join(os.tmpdir(),'msee-outside-'));
  try {
    await writeFile(path.join(root,'ok'),'repository evidence');
    // Allowed: the returned path is canonical and really points at the same file.
    const allowed=await confined(root,'ok');
    assert.equal(allowed,path.join(realpathSync.native(root),'ok'));
    assert.equal(realpathSync.native(allowed),allowed);
    assert.equal(await readFile(allowed,'utf8'),'repository evidence');
    assert.equal(await confined(root,'.'),realpathSync.native(root));

    // Rejected: traversal out of the worktree.
    await assert.rejects(confined(root,'../outside'),/outside repository/);
    await assert.rejects(confined(root,path.join(outside,'secret')),/outside repository/);
    await assert.rejects(confined(root,'sub/../../outside'),/outside repository/);

    // Rejected: a link inside the worktree cannot be traversed, even when it points out.
    await symlink(outside,path.join(root,'escape'),process.platform==='win32'?'junction':'dir');
    await assert.rejects(confined(root,'escape'),/Symlink or junction/);
    await assert.rejects(confined(root,'escape/secret'),/Symlink or junction/);
    await assert.rejects(rejectLinks(root),/Symlink or junction/);

    // Rejected: .git is never reachable, however it is spelled.
    await assert.rejects(confined(root,'.git'),/Invalid repository path/);
    await assert.rejects(confined(root,'.git/config'),/Invalid repository path/);
    await assert.rejects(confined(root,'sub/.GIT/config'),/Invalid repository path/);

    // Rejected: a missing path is refused explicitly rather than surfacing a raw ENOENT.
    await assert.rejects(confined(root,'absent'),/Path does not exist/);
  } finally {await rm(root,{recursive:true,force:true});await rm(outside,{recursive:true,force:true});}
});
test('the same directory is not rejected for being spelled differently',async()=>{
  // Regression: this is the failure CI caught. A GitHub runner hands out a short name
  // (RUNNER~1) while the resolver returns the long one (runneradmin), so comparing a
  // request against the worktree lexically rejected a path that is inside the worktree.
  const parent=await mkdtemp(path.join(os.tmpdir(),'msee-alias-'));
  const root=path.join(parent,'repo');
  const alias=path.join(parent,'shortname');
  await mkdir(root);
  await symlink(root,alias,process.platform==='win32'?'junction':'dir');
  try {
    await writeFile(path.join(root,'ok'),'repository evidence');
    const canonical=realpathSync.native(root);
    assert.equal(realpathSync.native(alias),canonical);
    // The tools and the confinement check must share one spelling of the worktree.
    assert.equal(worktreeRoot(alias),worktreeRoot(root));
    assert.equal(worktreeRoot(root),canonical);
    // Relative request: the alias and the real name must agree.
    assert.equal(await confined(alias,'ok'),await confined(root,'ok'));
    // Absolute request spelled through the alias names the same file inside the worktree.
    assert.equal(await confined(root,path.join(alias,'ok')),path.join(canonical,'ok'));
    // And an alias still cannot reach outside the worktree it points at.
    await assert.rejects(confined(root,path.join(alias,'..','shortname','..','..')),/outside repository/);
  } finally {await rm(parent,{recursive:true,force:true});}
});
test('redacted content cannot be used as a complete recording',async()=>{
  assert.equal(redact({authorization:'test',text:'sk-abcdefghijklmnop'}).changed,true);
  const root=await mkdtemp(path.join(os.tmpdir(),'msee-redact-'));
  try { const b=fresh('sk-abcdefghijklmnop','p','p','m','c');const file=path.join(root,'x.json');
    const saved=await save(file,b);assert.equal(saved.complete,false);await assert.rejects(load(file),Incomplete);
  } finally {await rm(root,{recursive:true,force:true});}
});
test('models.json command execution is refused',async()=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'msee-models-'));
  try {
    const file=path.join(root,'models.json');
    await writeFile(file,JSON.stringify({providers:{p:{baseUrl:'https://example.invalid/v1',api:'openai-completions',apiKey:"!op read 'x'",models:[{id:'m'}]}}}));
    await assert.rejects(assertNoCommandExecution(file),/command execution is not allowed/);
    await writeFile(file,JSON.stringify({providers:{p:{baseUrl:'https://example.invalid/v1',api:'openai-completions',apiKey:'$MSEE_PI_GATEWAY_KEY',models:[{id:'m'}]}}}));
    await assertNoCommandExecution(file);
  } finally {await rm(root,{recursive:true,force:true});}
});
test('snapshot tool source lets a changed prompt explore the pinned checkout',async()=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'msee-snapshot-'));
  try {
    await writeFile(path.join(root,'a.txt'),'alpha evidence');
    await writeFile(path.join(root,'b.txt'),'beta evidence');
    const parent=fresh('Investigate','Read a.txt.','test','test','abc');
    let call=0;
    await run({bundle:parent,root,stream:()=> ++call===1
      ? response([{type:'toolCall',id:'c1',name:'read',arguments:{path:'a.txt'}}],'toolUse')
      : response([{type:'text',text:'alpha'}])});
    assert.equal(parent.status,'succeeded',parent.reason);
    // Same prompt, different tool arguments: the strict tape would refuse this.
    const strict=fresh(parent.task,parent.prompt,'test','test','abc');strict.mode='regress';strict.parent=parent.id;
    await run({bundle:strict,parent,stream:()=>response([{type:'toolCall',id:'c2',name:'read',arguments:{path:'b.txt'}}],'toolUse')});
    assert.equal(strict.verdict,'inconclusive');
    assert.equal(strict.cause?.code,'missing_recorded_response');
    const snapshotRun=fresh(parent.task,'Read b.txt instead.','test','test','abc');snapshotRun.mode='regress';snapshotRun.parent=parent.id;
    snapshotRun.toolSource='snapshot';
    await run({bundle:snapshotRun,parent,root,toolSource:'snapshot',stream:()=> ++call<=3
      ? response([{type:'toolCall',id:'c3',name:'read',arguments:{path:'b.txt'}}],'toolUse')
      : response([{type:'text',text:'beta'}])});
    assert.equal(snapshotRun.status,'succeeded',snapshotRun.reason);
    assert.equal(snapshotRun.final,'beta');
    const toolStep=snapshotRun.steps.find(s=>s.kind==='tool_call')!;
    assert.equal(toolStep.source,'live');
    assert.match(JSON.stringify(toolStep.output),/beta evidence/);
    const protocol=payload(snapshotRun);
    assert.equal(protocol.events[2].effect_source,'live');
    assert.equal(snapshotRun.toolSource,'snapshot');
    // Snapshot mode still refuses paths outside the pinned checkout.
    const escape=fresh('t','p','test','test','abc');escape.mode='regress';escape.parent=parent.id;escape.toolSource='snapshot';
    await run({bundle:escape,parent,root,toolSource:'snapshot',stream:()=>response([{type:'toolCall',id:'c4',name:'read',arguments:{path:'../outside.txt'}}],'toolUse')});
    assert.equal(escape.verdict,'error');
  } finally {await rm(root,{recursive:true,force:true});}
});
test('audit separates a right answer wrapped in prose from a wrong answer',()=>{
  const spec={id:'t',answer:false,evidence:[{path:'a.py',line:2,quote:'x = 1'}]};
  const base={version:1,id:'b',started:'',ended:'',task:'',prompt:'p',promptHash:'',provider:'p',model:'m',
    commit:'c',piVersion:'0.85.1',thinking:'off',mode:'regress',steps:[],complete:true,status:'succeeded'} as any;
  const claim={claims:[{id:'t',answer:false,evidence:[{path:'a.py',line:2,quote:'x = 1'}]}]};
  assert.deepEqual(extractJson('{"a":{"b":[1,2]}} trailing'),{a:{b:[1,2]}});
  assert.equal(extractJson('no json here'),undefined);
  assert.equal(classify({...base,final:JSON.stringify(claim)},spec).kind,'pass');
  assert.equal(classify({...base,final:'结论如下：' + JSON.stringify(claim) + '\n原因：……'},spec).kind,'format_only');
  const wrong={claims:[{id:'t',answer:true,evidence:[{path:'a.py',line:2,quote:'x = 1'}]}]};
  assert.equal(classify({...base,final:JSON.stringify(wrong)},spec).kind,'wrong_answer');
  const badRef={claims:[{id:'t',answer:false,evidence:[{path:'a.py',line:9,quote:'x = 1'}]}]};
  assert.equal(classify({...base,final:JSON.stringify(badRef)},spec).kind,'bad_evidence');
  assert.equal(classify({...base,final:'I could not determine this.'},spec).kind,'no_json');
});
test('model budget, truncation and early replay termination are diagnostic',async()=>{
  const b=fresh('t','p','t','t','c');b.mode='regress';
  await run({bundle:b,stream:()=>response([{type:'text',text:'cut'}],'length')});
  assert.equal(b.verdict,'inconclusive');assert.equal(b.cause?.code,'truncated_context');
  const parent=fresh('t','p','t','t','c');parent.steps=[{kind:'tool_call',name:'read',input:{path:'x'},output:{},duration_ms:0}];
  const bad=clone(parent);bad.steps=[];bad.mode='reproduce';
  await run({bundle:bad,parent});assert.equal(bad.verdict,'inconclusive');
});

test('budgets stop the actual SDK loop and provider errors remain errors',async()=>{
  const parent=fresh('t','p','t','t','c');
  parent.steps=[{kind:'tool_call',name:'read',input:{path:'x'},output:{content:[{type:'text',text:'x'}]},duration_ms:0}];
  const b=fresh('t','p','t','t','c');b.mode='regress';
  await run({bundle:b,parent,maxModels:1,stream:()=>response([{type:'toolCall',id:'c',name:'read',arguments:{path:'x'}}],'toolUse')});
  assert.equal(b.status,'aborted');assert.equal(b.reason,'model_budget_exceeded');
  const failed=fresh('t','p','t','t','c');failed.mode='regress';
  await run({bundle:failed,stream:()=>{throw new Error('provider unavailable');}});
  assert.equal(failed.verdict,'error');assert.equal(failed.reason,'provider unavailable');
  assert.equal(failed.steps[0].error,'provider unavailable');
  const timed=fresh('t','p','t','t','c');timed.mode='regress';
  await run({bundle:timed,timeoutMs:15,stream:(_m:any,_c:any,o:any)=>{
    const stream=createAssistantMessageEventStream();
    o.signal.addEventListener('abort',()=>{
      const m:any={role:'assistant',content:[],stopReason:'aborted',errorMessage:'cancelled',timestamp:1};
      stream.push({type:'error',reason:'aborted',error:m});stream.end(m);
    });return stream;
  }});
  assert.equal(timed.status,'aborted');assert.equal(timed.reason,'time_budget_exceeded');
});
