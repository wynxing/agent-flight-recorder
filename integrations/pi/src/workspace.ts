import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, rmdir } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';

const exec = promisify(execFile);
async function git(repo:string,args:string[]) { return (await exec('git',['-C',repo,...args],{maxBuffer:1024*1024,windowsHide:true})).stdout.trim(); }
export async function snapshot(repo:string,ref:string) {
  const commit=await git(repo,['rev-parse','--verify','--end-of-options',`${ref}^{commit}`]);
  const parent=await mkdtemp(path.join(os.tmpdir(),'msee-pi-checkout-'));
  const root=path.join(parent,'repo');
  try { await git(repo,['worktree','add','--detach',root,commit]); }
  catch(e) { await rmdir(parent);throw e; }
  return {root,commit,async close() {
    if (path.dirname(parent)!==os.tmpdir() || !path.basename(parent).startsWith('msee-pi-checkout-') || path.dirname(root)!==parent) throw new Error('Invalid cleanup target');
    if (await git(root,['status','--porcelain','--untracked-files=all'])) throw new Error(`Target checkout changed; retained for inspection: ${root}`);
    await git(repo,['worktree','remove',root]);
    await rmdir(parent);
  }};
}
