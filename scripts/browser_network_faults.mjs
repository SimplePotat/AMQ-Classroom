import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve, join} from 'node:path';
import {randomBytes} from 'node:crypto';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root=resolve(import.meta.dirname,'..'), data=await mkdtemp(join(tmpdir(),'classroom-network-faults-'));
const python=process.env.PYTHON || join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.QA_PORT || 18039), base=`http://127.0.0.1:${port}`;
const key=randomBytes(18).toString('hex'), scenario=process.env.QA_SCENARIO || 'all';
execFileSync(python,[join(root,'scripts/make_test_catalog.py'),data]);
const server=spawn(python,['run_classroom.py','--host','127.0.0.1','--port',String(port),'--advertise',base],{
  cwd:root,env:{...process.env,CLASSROOM_DATA_DIR:data,CLASSROOM_HOST_KEY:key,CLASSROOM_PREWARM:'0'},stdio:['ignore','ignore','pipe']});
let logs='', browser;
server.stderr.on('data',chunk=>{logs+=chunk;});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms)), errors=[];
try {
  for(let i=0;i<100;i++){
    try{if((await fetch(base)).ok)break;}catch{}
    if(i===99)throw new Error('Temporary server did not start');
    await delay(100);
  }
  browser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required']});
  const hostContext=await browser.newContext({viewport:{width:1280,height:900}});
  const playerContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true});
  const host=await hostContext.newPage(), player=await playerContext.newPage();
  for(const page of [host,player])page.on('pageerror',error=>errors.push(error.message));
  async function api(page,path='/api/state',body){return page.evaluate(async({path,body})=>{
    const token=document.cookie.split('; ').find(c=>c.startsWith('csrftoken='))?.split('=')[1];
    const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-QA-Direct':'1',...(body?{'Content-Type':'application/json','X-CSRFToken':token}:{})},body:body?JSON.stringify(body):undefined});
    return {status:response.status,data:await response.json()};
  },{path,body});}
  async function waitState(predicate,description,attempts=120){
    let state;
    for(let i=0;i<attempts;i++){
      state=(await api(host)).data;
      if(predicate(state))return state;
      await delay(100);
    }
    throw new Error(`${description}: phase=${state?.phase}, round=${state?.round}`);
  }
  async function control(action,extra={}){
    const state=(await api(host)).data;
    const result=await api(host,'/api/host',{action,roundId:state.roundId,...extra});
    assert.equal(result.status,200,JSON.stringify(result));
  }
  async function prepare({rounds=2,autoAdvance=true}={}){
    await control('reset');
    await control('prepare',{settings:{source:'local',rounds,guessSeconds:90,revealSeconds:5,randomStart:false,fullReveal:true,autoAdvance}});
    await waitState(s=>s.phase==='ready','prepare local fixtures');
  }
  const enabled=name=>scenario==='all'||scenario===name;
  await host.goto(`${base}/host`);
  await host.getByLabel('Host key').fill(key);
  await host.getByRole('button',{name:'Open host desk'}).click();
  await host.getByRole('heading',{name:'Set the stage.',exact:true}).waitFor();
  await player.goto(base);
  assert.equal((await api(player,'/api/auth',{action:'register',name:'Fault player',password:'classroom-test',mal:'fault_private_mal',avatar:0})).status,200);

  if(enabled('commit')){
    await prepare();
    const dropped={begin:0,'reveal-ended':0};
    const dropCommittedResponse=async route=>{
      const action=route.request().postDataJSON()?.action;
      if(Object.hasOwn(dropped,action)&&dropped[action]++===0){
        const response=await route.fetch();
        assert.equal(response.status(),200,'Fault injection must happen after a successful commit');
        await route.abort('failed');
      }else await route.continue();
    };
    await host.route('**/api/host',dropCommittedResponse);
    await control('start');
    const first=await waitState(s=>s.phase==='guess','guess after committed begin lost its response');
    await host.getByRole('button',{name:'Reveal answer',exact:true}).waitFor();
    await control('reveal');
    await host.locator('#stage.revealed').waitFor();
    await host.locator('video').evaluate(video=>{video.currentTime=video.duration-0.25;});
    const second=await waitState(s=>s.phase==='guess'&&s.round===2,'advance after committed ending lost its response');
    assert.notEqual(second.roundId,first.roundId);
    assert.equal(second.history.length,1,'A lost response duplicated history');
    assert.ok(dropped.begin>=1&&dropped['reveal-ended']>=1);
    await host.unroute('**/api/host',dropCommittedResponse);
    console.log('PASS: begin/ending response loss after server commit does not duplicate rounds or stick playback.');
  }

  if(enabled('offline')){
    await prepare(); await control('start');
    await waitState(s=>s.phase==='guess','offline test guess');
    await player.getByLabel('Your anime guess').fill('private offline draft');
    await playerContext.setOffline(true);
    await player.locator('#connection.offline').waitFor({state:'attached'});
    await player.waitForFunction(()=>document.querySelector('#guess-form button').disabled);
    assert.equal(await player.getByLabel('Your anime guess').inputValue(),'private offline draft');
    await delay(1200);
    await playerContext.setOffline(false);
    await player.locator('#connection:not(.offline)').waitFor({state:'attached'});
    assert.equal(await player.getByLabel('Your anime guess').inputValue(),'private offline draft');
    await player.getByRole('button',{name:'Send answer'}).click();
    await player.getByText('✓ Received: private offline draft. You can still change it.',{exact:true}).waitFor();

    let failPolls=true;
    const failUiPoll=route=>failPolls&&!route.request().headers()['x-qa-direct']?route.abort('failed'):route.continue();
    await host.route('**/api/state',failUiPoll);
    await host.getByText('RECONNECTING',{exact:true}).waitFor();
    await control('reveal');
    failPolls=false;
    await host.getByText('LAN CONNECTED',{exact:true}).waitFor();
    await host.locator('#stage.revealed').waitFor();
    await host.unroute('**/api/state',failUiPoll);
    console.log('PASS: player offline drafts and host state recover after temporary polling failures.');
  }

  if(enabled('stale')){
    await prepare(); await control('start');
    const first=await waitState(s=>s.phase==='guess','delayed submission guess');
    await player.getByLabel('Your anime guess').fill('delayed old answer');
    let pending, release;
    const intercepted=new Promise(resolve=>{release=resolve;});
    const holdGuess=async route=>{
      if(route.request().postDataJSON()?.action==='guess'){
        pending=route; await intercepted; await route.continue();
      }else await route.continue();
    };
    await player.route('**/api/player',holdGuess);
    await player.getByRole('button',{name:'Send answer'}).click();
    for(let i=0;!pending&&i<50;i++)await delay(50);
    assert.ok(pending,'Guess was not held');
    await control('reveal'); await control('next');
    const next=await waitState(s=>s.phase==='guess'&&s.round===2,'next round before delayed submit');
    const delayedResponse=player.waitForResponse(response=>response.url().endsWith('/api/player')&&response.request().postDataJSON()?.action==='guess');
    release();
    assert.equal((await delayedResponse).status(),409,'Delayed guess should be explicitly rejected');
    await player.unroute('**/api/player',holdGuess);
    await player.getByLabel('Your anime guess').waitFor();
    await delay(1200);
    const state=(await api(player)).data;
    assert.notEqual(state.roundId,first.roundId);
    assert.equal(state.roundId,next.roundId);
    assert.equal(state.myGuess,'','Delayed guess was applied to a different round');
    assert.equal(await player.getByLabel('Your anime guess').inputValue(),'','Old draft leaked into a new round');
    console.log('PASS: delayed player submission cannot score or become a draft in the next round.');
  }

  if(enabled('identity')){
    await prepare(); await control('start');
    await waitState(s=>s.phase==='guess','cross-tab identity guess');
    await player.getByLabel('Your anime guess').fill('old account unsent draft');
    const sibling=await playerContext.newPage();
    sibling.on('pageerror',error=>errors.push(error.message));
    await sibling.goto(base);
    await sibling.getByRole('button',{name:'Edit profile',exact:false}).waitFor();
    await player.getByRole('button',{name:'Edit profile',exact:false}).click();
    await player.locator('#edit-name').fill('Unsaved old profile');
    assert.equal((await api(sibling,'/api/logout',{role:'player'})).status,200);
    await player.locator('#auth-form').waitFor();
    assert.equal(await player.locator('#profile-dialog[open]').count(),0,'Cross-tab logout left a private profile dialog open over sign-in');
    assert.equal(await player.locator('#profile-dialog').textContent(),'','Old private profile fields remained in the shared device DOM');
    assert.equal((await api(sibling,'/api/auth',{action:'register',name:'Second fault player',password:'classroom-test',mal:'',avatar:1})).status,200);
    await player.getByRole('button',{name:'Edit profile',exact:false}).waitFor();
    assert.equal(await player.locator('body').textContent().then(text=>text.includes('fault_private_mal')),false);
    await prepare(); await control('start');
    await waitState(s=>s.phase==='guess','new account next game');
    await player.getByLabel('Your anime guess').waitFor();
    assert.equal(await player.getByLabel('Your anime guess').inputValue(),'','A different account inherited the prior draft');
    await sibling.close();
    console.log('PASS: cross-tab logout/re-registration closes private forms and clears unsent account drafts.');
  }

  if(enabled('hang')){
    await prepare({rounds:1}); await control('start');
    await waitState(s=>s.phase==='guess','hung poll test guess');
    await host.getByRole('button',{name:'Reveal answer',exact:true}).waitFor();
    let held, release;
    const blocked=new Promise(resolve=>{release=resolve;});
    const holdFirstPoll=async route=>{
      if(!route.request().headers()['x-qa-direct']&&!held){
        held=route;
        await blocked;
        try{await route.continue();}catch{/* The polling deadline can cancel this request first. */}
      }else await route.continue();
    };
    await host.route('**/api/state',holdFirstPoll);
    try{
      for(let i=0;!held&&i<50;i++)await delay(50);
      assert.ok(held,'UI poll was not held');
      await control('reveal');
      await host.locator('#stage.revealed').waitFor({timeout:12000});
    }finally{
      release();
      await host.unroute('**/api/state',holdFirstPoll,{behavior:'wait'});
    }
    console.log('PASS: an unanswered state fetch cannot permanently block polling and command refresh.');
  }
  assert.deepEqual(errors,[],`Browser errors: ${errors.join(', ')}`);
}catch(error){console.error(logs);throw error;}
finally{
  if(browser)await browser.close();
  server.kill('SIGTERM');
  await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
  await rm(data,{recursive:true,force:true});
}
