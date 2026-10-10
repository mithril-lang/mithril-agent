#!/usr/bin/env node
import {spawn} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,statSync,realpathSync,existsSync,openSync,closeSync,unlinkSync} from 'node:fs';
import {hostname,tmpdir} from 'node:os';
import {dirname,resolve,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes} from 'node:crypto';
import {profile,digest,seal,admit,validateOwner} from './gateway-contract.mjs';

const root = realpathSync(resolve(dirname(fileURLToPath(import.meta.url)),'../..'));
const [command,...argv] = process.argv.slice(2);
const opts = {};
for (let i=0;i<argv.length;i+=2) {
  if (!['--state','--owner','--receipt'].includes(argv[i]) || !argv[i+1]) throw Error('Expected --state/--owner/--receipt PATH');
  opts[argv[i].slice(2)] = resolve(argv[i+1]);
}
const quote = s => "'" + String(s).replaceAll("'", "'\\''") + "'";
const env = {PATH:process.env.PATH,HOME:process.env.HOME};
function run(cmd,args,{input,log,timeout=1800000}={}) {
  return new Promise((ok,bad)=>{
    const p=spawn(cmd,args,{cwd:root,env,stdio:['pipe','pipe','pipe']});
    const chunks=[]; let size=0;
    const timer=setTimeout(()=>{p.kill('SIGTERM');bad(Error(`${cmd}: timeout`));},timeout);
    for (const stream of [p.stdout,p.stderr]) stream.on('data',x=>{
      if (log) log.write(x);
      chunks.push(x);size+=x.length;
      if(size>128*1024**2){p.kill('SIGTERM');bad(Error('Output limit exceeded'));}
    });
    p.on('error',bad);
    p.on('close',code=>{clearTimeout(timer);const b=Buffer.concat(chunks);code===0?ok(b):bad(Error(`${cmd} exited ${code}: ${b.toString().slice(-3000)}`));});
    p.stdin.on('error',()=>{});p.stdin.end(input);
  });
}
const git=async (...args)=>(await run('git',args)).toString().trim();
const privateFile=path=>{const s=statSync(path);if(!s.isFile()||s.uid!==process.getuid()||(s.mode&0o077))throw Error('Owner-only external file required');return readFileSync(path);};
const recipeFiles=['Dockerfile','scripts/standalone/gateway.mjs','scripts/standalone/gateway-contract.mjs','scripts/standalone/gateway-image-probe.py','scripts/standalone/gateway-http-probe.py','scripts/standalone/gateway-test.Dockerfile'];
const recipeDigest=digest({profile,files:recipeFiles.map(path=>[path,digest(readFileSync(join(root,path)).toString())])});
let lockFd,lockPath;
try {
  if(!['plan','verify','admit'].includes(command))throw Error('Usage: gateway.mjs plan | verify/admit --state PATH --owner PATH [--receipt PATH]');
  const sha=await git('rev-parse','HEAD');
  if(command==='plan'){console.log(JSON.stringify({repository:'mithril-lang/mithril-agent',sha,recipeDigest,...profile,productionEligible:false},null,2));process.exit(0);}
  if(!opts.state||!opts.owner)throw Error('External --state and --owner are required');
  if(opts.state===root||opts.state.startsWith(root+'/')||opts.owner.startsWith(root+'/'))throw Error('Owner and state must be outside source');
  mkdirSync(opts.state,{recursive:true,mode:0o700});
  const stateStat=statSync(opts.state);if(stateStat.uid!==process.getuid()||(stateStat.mode&0o077))throw Error('State must be private');
  const owner=validateOwner(JSON.parse(privateFile(opts.owner)),hostname(),root);
  const ownerIdentity=digest(owner);
  const identity={repository:owner.repository,sha,recipeDigest,ownerIdentity,profile:profile.name};
  const keyPath=join(opts.state,'authority.key');
  if(!existsSync(keyPath))writeFileSync(keyPath,randomBytes(32),{mode:0o600,flag:'wx'});
  const key=privateFile(keyPath);if(key.length!==32)throw Error('Invalid authority key');
  if(command==='admit'){
    if(!opts.receipt)throw Error('--receipt required');
    console.log(JSON.stringify(admit(JSON.parse(privateFile(opts.receipt)),key,identity),null,2));process.exit(0);
  }
  if(await git('status','--porcelain'))throw Error('Commit changes and use a clean exact source checkout');
  const remote=await git('remote','get-url','origin');
  if(!/github.com[:/]mithril-lang\/mithril-agent(?:\.git)?$/.test(remote))throw Error('Unexpected source repository');
  lockPath=join(opts.state,'verify.lock');lockFd=openSync(lockPath,'wx',0o600);
  const id=sha.slice(0,12)+'-'+randomBytes(4).toString('hex');
  const receiptPath=join(opts.state,id+'.json');
  const {createWriteStream}=await import('node:fs');
  const log=createWriteStream(join(opts.state,id+'.log'),{mode:0o600});
  const dockerArgs=owner.socket?['--host','unix://'+owner.socket]:[];
  const remoteCommand=args=>['-o','BatchMode=yes','-o','ConnectTimeout=10','gad',args.map(quote).join(' ')];
  const docker=async (...args)=>owner.runner==='gad'?run('ssh',remoteCommand(['docker',...dockerArgs,...args]),{log}):run('docker',[...dockerArgs,...args],{log});
  const shell=async (script,input)=>owner.runner==='gad'?run('ssh',remoteCommand(['bash','-ec',script]),{input,log}):run('bash',['-ec',script],{input,log});
  let source,imageId,testName,volume,container;const checks=[];
  let stage='runner-preflight';
  try {
    const info=JSON.parse((await docker('info','--format','{{json .}}')).toString());
    if(info.Architecture!=='x86_64')throw Error('This profile needs the configured native linux/amd64 runner');
    const rootBytes=Number((await shell(`df -Pk ${quote(info.DockerRootDir)} | awk 'NR==2 {print $4*1024}'`)).toString().trim());
    if(rootBytes<profile.minimumFreeBytes)throw Error(`Runner free capacity ${rootBytes} bytes is below ${profile.minimumFreeBytes}`);
    source=(await shell(`mktemp -d ${owner.socket?'/dev/shm':'/tmp'}/mithril-agent-ci.XXXXXXXX`)).toString().trim();
    if(!/^\/(?:tmp|dev\/shm)\/mithril-agent-ci\.[A-Za-z0-9]+$/.test(source))throw Error('Unexpected scratch directory');
    const archive=await run('git',['archive','--format=tar.gz',sha]);
    await shell(`tar -xz -C ${quote(source)}`,archive);
    await shell(`cd ${quote(source)}; python3 scripts/write_install_stamp.py --output install-stamp.json --distribution docker --update-mechanism external --source ci --commit ${quote(sha)}; python3 scripts/ci/check_profile_archive_boundary.py`);
    stage='image-build';const image='mithril-agent-gateway:'+id;
    await docker('build','--platform',profile.platform,'--network','host','--label','org.opencontainers.image.revision='+sha,'-t',image,source);
    imageId=(await docker('image','inspect','--format','{{.Id}}',image)).toString().trim();
    checks.push('image-build');
    stage='offline-regressions';testName='mithril-agent-gateway-test:'+id;
    await shell(`cp ${quote(source+'/.dockerignore')} ${quote(source+'/.dockerignore.runtime')}; printf '.git\n' > ${quote(source+'/.dockerignore')}`);
    await docker('build','--network','host','-f',source+'/scripts/standalone/gateway-test.Dockerfile','--build-arg','VERIFIED_IMAGE='+image,'-t',testName,source);
    const testLog=(await docker('run','--rm','--network','none','--cpus','2','--memory','4g','--pids-limit','256','--cap-drop','ALL','--security-opt','no-new-privileges',testName)).toString();
    if(!/Summary:.*[1-9][0-9]* tests passed, 0 failed/.test(testLog))throw Error('Test execution did not report successful real tests');
    checks.push('offline-regressions');
    stage='image-runtime';volume='mithril-agent-gateway-volume-'+id;container='mithril-agent-gateway-'+id;
    await docker('volume','create',volume);
    await docker('run','-d','--name',container,'--network','none','--cpus','2','--memory','4g','--pids-limit','256','--mount','type=volume,src='+volume+',dst=/opt/data','-e','API_SERVER_KEY=standalone-fixture-key-not-a-production-secret','-e','API_SERVER_HOST=127.0.0.1','-e','API_SERVER_PORT=7860',imageId,'gateway','run');
    const probe=readFileSync(join(root,'scripts/standalone/gateway-image-probe.py'));
    let ready=false;
    for(let n=0;n<60;n++){
      try{const b=await docker('exec','-u','hermes',container,'test','-w','/opt/data');ready=true;break;}catch{}
      await new Promise(ok=>setTimeout(ok,1000));
    }
    if(!ready)throw Error('Image did not prepare its unprivileged home');
    const execProbe=async restart=>{
      const args=['docker',...dockerArgs,'exec','-i','-u','hermes',container,'/opt/hermes/.venv/bin/python','-',sha,...(restart?['restart']:[])];
      return owner.runner==='gad'?run('ssh',remoteCommand(args),{input:probe,log}):run(args.shift(),args,{input:probe,log});
    };
    await execProbe(false);
    const httpProbe=readFileSync(join(root,'scripts/standalone/gateway-http-probe.py'));
    const httpArgs=['docker',...dockerArgs,'exec','-i','-u','hermes',container,'/opt/hermes/.venv/bin/python','-','standalone-fixture-key-not-a-production-secret'];
    const httpRun=()=>owner.runner==='gad'?run('ssh',remoteCommand(httpArgs),{input:httpProbe,log}):run('docker',httpArgs.slice(1),{input:httpProbe,log});
    await httpRun();await docker('restart',container);await execProbe(true);await httpRun();
    checks.push('image-runtime');
    if(await git('rev-parse','HEAD')!==sha || await git('status','--porcelain'))throw Error('Source changed during qualification');
    const r={...identity,status:'success',finishedAt:new Date().toISOString(),runner:owner.runner,imageId,checks,productionEligible:false};
    writeFileSync(receiptPath,JSON.stringify(seal(r,key),null,2)+'\n',{mode:0o600});
    console.log(JSON.stringify({receipt:receiptPath,...r},null,2));
  }catch(error){
    writeFileSync(receiptPath,JSON.stringify(seal({...identity,status:'failed',finishedAt:new Date().toISOString(),stage,imageId:imageId??null,checks,error:String(error.message),productionEligible:false},key),null,2)+'\n',{mode:0o600});
    throw Error(`${stage}: ${error.message}; failure receipt: ${receiptPath}`);
  }finally{
    if(container)await docker('rm','-f',container).catch(()=>{});
    if(volume)await docker('volume','rm',volume).catch(()=>{});
    if(testName)await docker('image','rm',testName).catch(()=>{});
    if(source)await shell(`rm -rf -- ${quote(source)}`).catch(()=>{});
    log.end();
  }
}catch(error){console.error(error.message);process.exitCode=1;}
finally{if(lockFd!==undefined){closeSync(lockFd);unlinkSync(lockPath);}}
